"""Run-scoped deterministic service for role-limited evaluation access."""

from __future__ import annotations

import asyncio
import errno
import secrets
import socket
from contextlib import suppress
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import TypeAdapter

from vs_evaluation.agent_evidence import EvidenceKind, TrustedEvidence
from vs_evaluation.agent_models import (
    EVALUATION_ACCESS_STATE_PATH,
    MAX_AGENT_AWAIT_S,
    AgentEvaluationCall,
    AgentEvaluationReply,
    AvailabilityCall,
    AvailabilityReply,
    AwaitCall,
    AwaitProfilerCall,
    AwaitReply,
    CancelCall,
    CanceledReply,
    CancelProfilerCall,
    DispatchProfilerCall,
    EvaluationAgentRole,
    EvaluationAgentState,
    EvaluationGrant,
    EvaluationOperationObservation,
    EvaluationOperationSnapshot,
    EvaluationStillRunning,
    EvidenceCall,
    EvidencePreflightCheck,
    EvidencePreflightDecision,
    EvidencePreflightResolution,
    EvidenceReply,
    HandleAccess,
    HandleAssociation,
    ProfilerOperationsCall,
    ProfilerStatusCall,
    RepeatedFailure,
    RunOperationsCall,
    RunOperationsReply,
    RunStoppingReply,
    ScopeRelease,
    ScopeReleasedReply,
    SocketFailure,
    SocketSuccess,
    StatusCall,
    StatusReply,
    SubmitCall,
    SubmittedReply,
    SubmittedSemanticEvaluation,
)
from vs_evaluation.models import (
    AvailabilitySnapshot,
    AvailabilityState,
    EvaluationCompleted,
    EvaluationFailed,
    EvaluationState,
    EvaluationTimedOut,
    ResourceRequirements,
)
from vs_evaluation.profiler_service import ProfilerAgentUnavailableError
from vs_evaluation.repeated_failure import detect_repeated_failure
from vs_evaluation.scope_state import (
    EvaluationAdmissionStoppedError,
    ScopeClosingError,
    ScopeLifecycleStore,
    ScopePhase,
)
from vs_evaluation.settlements import (
    EvaluationDependencyError,
    ServiceEvaluationSettlements,
    SettlementErrorCode,
)
from vs_project.api import validate_socket_path

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from vs_evaluation.models import EvaluationAwaitResult, StoredEvaluation
    from vs_evaluation.profiler_service import ProfilerAgentService
    from vs_evaluation.settlements import EvaluationSettlements
    from vs_evaluation.state_namespace import EvaluationStateNamespace

_STATE_PATH = EVALUATION_ACCESS_STATE_PATH
_TERMINAL_EVALUATION_STATES = frozenset(
    {
        EvaluationState.SUCCEEDED,
        EvaluationState.FAILED,
        EvaluationState.CANCELED,
        EvaluationState.SUPERSEDED,
    }
)
_EVALUATION_CLEANUP_FAILED = "evaluation service cleanup failed"
_CALL_ADAPTER = TypeAdapter(AgentEvaluationCall)
_REPLY_ADAPTER = TypeAdapter(AgentEvaluationReply)
_MAX_FRAME_BYTES = 1_048_576


class EvaluationBackend(Protocol):
    """Resource-neutral coordinator capability consumed by the agent service."""

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        """Return current normalized availability."""
        ...

    async def submit_evidence(
        self,
        scope_id: str | None,
        kinds: tuple[EvidenceKind, ...],
        *,
        own: Callable[[SubmittedSemanticEvaluation], Awaitable[None]],
    ) -> SubmittedSemanticEvaluation:
        """Build and submit trusted execution for semantic evidence intent."""
        ...

    async def accepted_evidence(
        self,
        scope_id: str | None,
        kinds: tuple[EvidenceKind, ...],
    ) -> tuple[TrustedEvidence, ...]:
        """Return trust-boundary accepted evidence for the current scope snapshot."""
        ...

    async def drain_submissions(self, scope_id: str | None) -> None:
        """Join admitted submits after durable admission closure."""
        ...

    async def owned_handles(self, scope_id: str | None) -> tuple[str, ...]:
        """Read claimed ownership; ``None`` projects all run-owned resources."""
        ...

    async def inspect_snapshot(self, handle_id: str) -> StoredEvaluation | None:
        """Inspect external identity without submitting or cancelling work; None is unknown."""
        ...

    async def recorded_snapshot(self, handle_id: str) -> StoredEvaluation:
        """Read the durable request ownership, generation and terminal record."""
        ...

    async def recorded_submission(self, handle_id: str) -> SubmittedSemanticEvaluation | None:
        """Read immutable submitted identity, or None for legacy unknown identity."""
        ...

    async def recorded_status(self, handle_id: str) -> EvaluationState:
        """Read durable status without submitting or inspecting external work."""
        ...

    async def status(self, handle_id: str) -> EvaluationState:
        """Return a handle's current state."""
        ...

    async def operation_snapshot(self, handle_id: str) -> EvaluationOperationSnapshot:
        """Return trusted lifecycle and accepted-result state for one handle."""
        ...

    async def await_result(self, handle_id: str, timeout_s: float) -> EvaluationAwaitResult:
        """Wait at most the caller's bound without canceling on timeout."""
        ...

    async def cancel(self, handle_id: str) -> StoredEvaluation:
        """Request cancellation and return the durable record."""
        ...


class AccessErrorCode(StrEnum):
    """Stable authorization rejection reasons."""

    INVALID_GRANT = "invalid_grant"
    AVAILABILITY_READ_ONLY = "availability_read_only"
    JUDGE_READ_ONLY = "judge_read_only"
    KIND_DENIED = "kind_denied"
    KIND_UNSUPPORTED = "kind_unsupported"
    EVIDENCE_DENIED = "evidence_denied"
    UNKNOWN_HANDLE = "unknown_handle"
    HANDLE_DENIED = "handle_denied"
    CANCEL_DENIED = "cancel_denied"
    PROFILER_DENIED = "profiler_denied"
    RUN_OBSERVATION_DENIED = "run_observation_denied"


class EvaluationAgentAccessError(PermissionError):
    """A principal attempted an operation outside its granted capability."""

    def __init__(self, code: AccessErrorCode, detail: str | None = None) -> None:
        """Build a stable authorization diagnostic."""
        messages = {
            AccessErrorCode.INVALID_GRANT: "invalid evaluation capability",
            AccessErrorCode.AVAILABILITY_READ_ONLY: (
                "availability observer may inspect resource availability only"
            ),
            AccessErrorCode.JUDGE_READ_ONLY: "judge may read accepted evidence only",
            AccessErrorCode.KIND_DENIED: "role cannot request evidence kind",
            AccessErrorCode.KIND_UNSUPPORTED: (
                "this run's evaluation executor cannot produce evidence kind"
            ),
            AccessErrorCode.EVIDENCE_DENIED: "accepted evidence is unavailable to this role",
            AccessErrorCode.UNKNOWN_HANDLE: "unknown evaluation handle",
            AccessErrorCode.HANDLE_DENIED: "evaluation handle is not visible to this principal",
            AccessErrorCode.CANCEL_DENIED: "only a submitting requester may cancel its association",
            AccessErrorCode.PROFILER_DENIED: (
                "profiler-agent lifecycle is available to implementers only"
            ),
            AccessErrorCode.RUN_OBSERVATION_DENIED: (
                "run-wide trusted operations are unavailable to this capability"
            ),
        }
        message = messages[code]
        super().__init__(f"{message}: {detail}" if detail else message)


class EvaluationAgentProtocolError(ValueError):
    """The socket peer sent an invalid frame."""

    def __init__(self) -> None:
        """Build the fixed malformed-frame diagnostic."""
        super().__init__("evaluation request must be one bounded JSON line")


class EvaluationAgentSocketError(RuntimeError):
    """The run's private socket path is already owned by a live service."""

    def __init__(self, path: Path) -> None:
        """Name the colliding run-owned socket path."""
        super().__init__(f"evaluation service is already listening at {path}")


_SUBMISSION_KINDS = {
    EvaluationAgentRole.IMPLEMENTER: frozenset({EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK}),
    EvaluationAgentRole.PROFILER: frozenset({EvidenceKind.PROFILE}),
    EvaluationAgentRole.JUDGE: frozenset(),
    EvaluationAgentRole.ORCHESTRATOR: frozenset({EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK}),
    EvaluationAgentRole.PORTFOLIO_DISPATCH: frozenset(),
    EvaluationAgentRole.RUN_OBSERVER: frozenset(),
}

_AVAILABILITY_KINDS = {
    **_SUBMISSION_KINDS,
    EvaluationAgentRole.ORCHESTRATOR: frozenset(EvidenceKind),
    EvaluationAgentRole.PORTFOLIO_DISPATCH: frozenset(EvidenceKind),
    EvaluationAgentRole.RUN_OBSERVER: frozenset(EvidenceKind),
}


def submission_evidence_kinds(role: EvaluationAgentRole) -> frozenset[EvidenceKind]:
    """Return the authoritative semantic evidence authority for ``role``."""
    return _SUBMISSION_KINDS[role]


def decide_evidence_preflight(
    role: EvaluationAgentRole,
    prerequisites: tuple[EvidenceKind, ...],
    availability: AvailabilitySnapshot,
    accepted: tuple[TrustedEvidence, ...],
    *,
    delegated_evidence_kinds: frozenset[EvidenceKind] = frozenset(),
) -> EvidencePreflightDecision:
    """Resolve declared prerequisites without submitting work or invoking an agent."""
    accepted_kinds = {item.kind for item in accepted}
    collectable = submission_evidence_kinds(role) | delegated_evidence_kinds
    supported = set(availability.supported_evidence_kinds)
    checks: list[EvidencePreflightCheck] = []
    for evidence_kind in prerequisites:
        if evidence_kind in accepted_kinds:
            resolution = EvidencePreflightResolution.ACCEPTED
        elif evidence_kind not in collectable:
            resolution = EvidencePreflightResolution.UNAUTHORIZED
        elif evidence_kind.value not in supported:
            resolution = EvidencePreflightResolution.UNSUPPORTED
        elif availability.state is AvailabilityState.UNAVAILABLE:
            resolution = EvidencePreflightResolution.UNAVAILABLE
        else:
            resolution = EvidencePreflightResolution.COLLECTABLE
        checks.append(EvidencePreflightCheck(evidence_kind=evidence_kind, resolution=resolution))
    blocked = any(
        check.resolution
        not in {EvidencePreflightResolution.ACCEPTED, EvidencePreflightResolution.COLLECTABLE}
        for check in checks
    )
    return EvidencePreflightDecision(blocked=blocked, checks=tuple(checks))


def _never_stopping() -> bool:
    return False


class EvaluationAgentService:
    """Own authorization, durable handle access, and a private Unix socket."""

    def __init__(
        self,
        backend: EvaluationBackend,
        namespace: EvaluationStateNamespace,
        socket_path: Path,
        profiler_agents: ProfilerAgentService | None = None,
        stopping: Callable[[], bool] = _never_stopping,
    ) -> None:
        """Bind the semantic coordinator, project state, and private socket.

        *stopping* reports whether the run is stopping; while it is, new
        submissions and profiler dispatches return :class:`RunStoppingReply`.
        """
        self._stopping = stopping
        self._stopped = False
        self._backend = backend
        self._namespace = namespace
        self._socket_path = validate_socket_path(socket_path)
        self._profiler_agents = profiler_agents
        self._grants: dict[str, EvaluationGrant] = {}
        self._scoped_grants: dict[
            tuple[str, EvaluationAgentRole, str | None, bool, bool], EvaluationGrant
        ] = {}
        self._state_lock = asyncio.Lock()
        self._release_lock = asyncio.Lock()
        self._scopes = ScopeLifecycleStore(namespace)
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._clients: set[asyncio.Task[None]] = set()

    def settlements(self) -> EvaluationSettlements:
        """Create owned host observations without exposing service internals."""
        return ServiceEvaluationSettlements(self._backend, self._namespace)

    @property
    def socket_path(self) -> Path:
        """Return the private listening path used in MCP subprocess grants."""
        return self._socket_path

    def delegated_evidence_kinds(self, role: EvaluationAgentRole) -> frozenset[EvidenceKind]:
        """Return evidence kinds this role can collect through delegated agents."""
        if role is EvaluationAgentRole.IMPLEMENTER and self._profiler_agents is not None:
            return frozenset({EvidenceKind.PROFILE})
        return frozenset()

    def grant(
        self,
        *,
        principal_id: str,
        role: EvaluationAgentRole,
        scope_id: str | None,
        run_observer: bool = False,
        evaluation_suspension: bool = False,
    ) -> EvaluationGrant:
        """Return the stable process-local capability for one principal and scope."""
        key = (principal_id, role, scope_id, run_observer, evaluation_suspension)
        existing = self._scoped_grants.get(key)
        if existing is not None:
            return existing
        grant = EvaluationGrant(
            token=secrets.token_urlsafe(32),
            principal_id=principal_id,
            role=role,
            scope_id=scope_id,
            profiler_available=self._profiler_agents is not None,
            run_observer=run_observer,
            evaluation_suspension=evaluation_suspension,
        )
        self._grants[grant.token] = grant
        self._scoped_grants[key] = grant
        return grant

    async def start(self) -> None:
        """Start accepting strict one-request JSONL connections."""
        if self._server is not None:
            return
        await self.reconcile_scopes()
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._bind_listener(listener)
            listener.setblocking(False)  # noqa: FBT003  # lint-waiver: LW-092713 [FBT003]; socket.setblocking exposes a positional-only boolean in the standard library, so a keyword argument cannot express this call.
            self._server = await asyncio.start_unix_server(
                self._serve_client,
                sock=listener,
                limit=_MAX_FRAME_BYTES + 1,
            )
            self._socket_path.chmod(0o600)
        except BaseException:
            listener.close()
            self._unlink_owned_socket()
            raise

    def _bind_listener(self, listener: socket.socket) -> None:
        """Atomically claim the singleton path, replacing only a proven stale inode."""
        try:
            listener.bind(str(self._socket_path))
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            stale_identity = self._path_identity()
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(self._socket_path))
            except (ConnectionRefusedError, FileNotFoundError):
                if stale_identity is None or self._path_identity() != stale_identity:
                    raise EvaluationAgentSocketError(self._socket_path) from error
                self._socket_path.unlink(missing_ok=True)
                try:
                    listener.bind(str(self._socket_path))
                except OSError as bind_error:
                    if bind_error.errno == errno.EADDRINUSE:
                        raise EvaluationAgentSocketError(self._socket_path) from bind_error
                    raise
            else:
                raise EvaluationAgentSocketError(self._socket_path) from error
            finally:
                probe.close()
        stat = self._socket_path.stat()
        self._socket_identity = (stat.st_dev, stat.st_ino)
        listener.listen(socket.SOMAXCONN)

    def _path_identity(self) -> tuple[int, int] | None:
        try:
            stat = self._socket_path.stat()
        except FileNotFoundError:
            return None
        return stat.st_dev, stat.st_ino

    def _unlink_owned_socket(self) -> None:
        identity, self._socket_identity = self._socket_identity, None
        if identity is not None and self._path_identity() == identity:
            self._socket_path.unlink(missing_ok=True)

    def begin_settling(self) -> None:
        """Reject new dispatch while retaining observation and evidence access."""
        self._stopped = True

    async def close(self) -> None:
        """Stop requests, cancel remembered work, and remove the private socket."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        clients = tuple(self._clients)
        for client in clients:
            client.cancel()
        if clients:
            await asyncio.gather(*clients, return_exceptions=True)
        cancellation_error: BaseException | None = None
        try:
            await self.cancel_outstanding()
        except BaseException as exc:  # noqa: BLE001  # lint-waiver: LW-930066 [BLE001]; all independently owned resources must be released during cancellation; narrower catches would skip cleanup, while a wrapper would only move the same boundary.
            cancellation_error = exc
        if server is not None:
            await server.wait_closed()
        self._unlink_owned_socket()
        self._grants.clear()
        self._scoped_grants.clear()
        if cancellation_error is not None:
            raise cancellation_error

    async def cancel_outstanding(self) -> None:
        """Cancel every evaluation submitted through this service and unstarted profiles.

        A queued profiler operation is cancelled so that it never starts after
        a stop; a running profiler turn finishes within the run's grace period.
        """
        self._stopped = True
        await self._backend.drain_submissions(None)
        async with self._state_lock:
            state = (
                self._namespace.load_optional(_STATE_PATH, EvaluationAgentState)
                or EvaluationAgentState()
            )
        scopes = self._scopes.snapshot().scopes
        for scope in scopes:
            self._scopes.begin(scope.scope_id)
        handles = tuple(
            dict.fromkeys(
                (
                    *(item.handle_id for item in state.handles),
                    *(await self._backend.owned_handles(None)),
                )
            )
        )
        cancellations = await asyncio.gather(
            *(self._backend.cancel(handle) for handle in handles),
            *(() if self._profiler_agents is None else (self._profiler_agents.cancel_queued(),)),
            return_exceptions=True,
        )
        errors = [result for result in cancellations if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup(_EVALUATION_CLEANUP_FAILED, errors)

    async def dispatch(self, call: AgentEvaluationCall) -> AgentEvaluationReply:
        """Authorize and perform one typed request."""
        grant = self._require_grant(call.token)
        if isinstance(call, AvailabilityCall):
            kinds = self._authorized_availability_kinds(grant, call.evidence_kinds)
            snapshot = await self._backend.availability(ResourceRequirements())
            supported = tuple(
                kind.value for kind in kinds if kind.value in snapshot.supported_evidence_kinds
            )
            return AvailabilityReply(
                snapshot=snapshot.model_copy(update={"supported_evidence_kinds": supported})
            )
        if isinstance(call, SubmitCall):
            return await self._submit(call, grant)
        if isinstance(call, EvidenceCall):
            self._require_evidence_reader(grant)
            kinds = self._authorized_evidence_query(grant, call.evidence_kinds)
            return EvidenceReply(
                evidence=await self._backend.accepted_evidence(grant.scope_id, kinds)
            )
        if isinstance(call, RunOperationsCall):
            if not grant.run_observer:
                raise EvaluationAgentAccessError(AccessErrorCode.RUN_OBSERVATION_DENIED)
            return await self._run_operations()
        if isinstance(
            call,
            DispatchProfilerCall
            | ProfilerOperationsCall
            | ProfilerStatusCall
            | AwaitProfilerCall
            | CancelProfilerCall,
        ):
            if grant.role is not EvaluationAgentRole.IMPLEMENTER:
                raise EvaluationAgentAccessError(AccessErrorCode.PROFILER_DENIED)
            return await self._dispatch_profiler(call, grant)
        if grant.role in {
            EvaluationAgentRole.PORTFOLIO_DISPATCH,
            EvaluationAgentRole.RUN_OBSERVER,
        }:
            raise EvaluationAgentAccessError(AccessErrorCode.AVAILABILITY_READ_ONLY)
        if grant.role is EvaluationAgentRole.JUDGE:
            raise EvaluationAgentAccessError(AccessErrorCode.JUDGE_READ_ONLY)
        access = await self._require_observer(grant, call.handle_id)
        return await self._dispatch_handle(call, grant, access)

    async def _submit(
        self, call: SubmitCall, grant: EvaluationGrant
    ) -> SubmittedReply | RunStoppingReply | ScopeReleasedReply:
        """Submit authorized evidence collection, or refuse it while the run stops."""
        kinds = self._authorized_submission_kinds(grant, call.evidence_kinds)
        if self._stopped or self._stopping():
            return RunStoppingReply()
        if await self.scope_released(grant.scope_id):
            return ScopeReleasedReply()
        await self._require_supported(kinds)
        try:
            submitted = await self._backend.submit_evidence(
                grant.scope_id, kinds, own=lambda prepared: self._remember(prepared, grant, kinds)
            )
        except EvaluationAdmissionStoppedError:
            return RunStoppingReply()
        except ScopeClosingError:
            return ScopeReleasedReply()
        if grant.scope_id is not None and await self.scope_released(grant.scope_id):
            # The scope was released while this submission was in flight.
            await self._cancel_owned(grant.scope_id)
            return ScopeReleasedReply()
        return SubmittedReply(handle_id=submitted.handle_id)

    async def cancel_scope(self, scope_id: str) -> ScopeRelease:
        """Fence admission, reconcile owned resources, and commit completion.

        Interrupted cleanup leaves Closing durable. Every retry or restart
        repeats cancellation of resources still observed nonterminal.
        ``first_release`` describes intent creation, never controls cleanup.
        """
        async with self._release_lock:
            intent, first = self._scopes.begin(scope_id)
            if intent.phase is ScopePhase.CLOSED:
                return ScopeRelease(scope_id=scope_id, first_release=False)
            await self._backend.drain_submissions(scope_id)
            evaluations, profiler_operations = await self._cancel_owned(scope_id)
            self._scopes.complete(scope_id)
            return ScopeRelease(
                scope_id=scope_id,
                evaluations=evaluations,
                profiler_operations=profiler_operations,
                first_release=first,
            )

    async def reconcile_scopes(self) -> None:
        """Replay unfinished release intents before ordinary admission."""
        await self.reconcile_associations()
        for scope in self._scopes.snapshot().scopes:
            if scope.phase is ScopePhase.CLOSING:
                await self.cancel_scope(scope.scope_id)

    async def reconcile_associations(self) -> None:
        """Replay final-requester cancellation intent after interrupted explicit cancellation."""
        async with self._state_lock:
            state = self._namespace.load_optional(_STATE_PATH, EvaluationAgentState)
            if state is None:
                return
            for access in state.handles:
                if access.cancel_pending:
                    await self._finish_association_cancel(access)

    async def cancel_association(self, handle_id: str, scope_id: str) -> StoredEvaluation:
        """Release a host-authorized requester wait while preserving other captures."""
        await self._detach_requester(handle_id, scope_id=scope_id)
        return await self._backend.recorded_snapshot(handle_id)

    async def _detach_requester(
        self,
        handle_id: str,
        *,
        scope_id: str | None,
        principal_id: str | None = None,
        canonical_claimed: bool = False,
    ) -> bool:
        """Commit withdrawal before physical cancellation; keep pending intent until terminal."""
        async with self._state_lock:
            record = await self._backend.recorded_snapshot(handle_id)
            state = (
                self._namespace.load_optional(_STATE_PATH, EvaluationAgentState)
                or EvaluationAgentState()
            )
            access = next((item for item in state.handles if item.handle_id == handle_id), None)
            if access is None:
                # Host captures can predate agent access. Their immutable capture
                # scope remains the only owner until an association is recorded.
                if record.request.owner_scope == scope_id or canonical_claimed:
                    await self._backend.cancel(handle_id)
                    return True
                return False
            access = access.model_copy(
                update={
                    "associations": access.requesters(
                        legacy_generation=record.request.owner_generation
                    )
                }
            ).detach(scope_id=scope_id, principal_id=principal_id)
            if record.state in _TERMINAL_EVALUATION_STATES:
                access = access.model_copy(update={"cancel_pending": False})
            self._save_access(state, access)
            if not access.cancel_pending:
                return False
            await self._finish_association_cancel(access)
            return True

    def _save_access(self, state: EvaluationAgentState, access: HandleAccess) -> None:
        self._namespace.save(
            _STATE_PATH,
            EvaluationAgentState(
                handles=tuple(
                    access if item.handle_id == access.handle_id else item for item in state.handles
                )
            ),
        )

    async def _finish_association_cancel(self, access: HandleAccess) -> None:
        record = await self._backend.cancel(access.handle_id)
        if record.state in _TERMINAL_EVALUATION_STATES:
            state = (
                self._namespace.load_optional(_STATE_PATH, EvaluationAgentState)
                or EvaluationAgentState()
            )
            current = next(item for item in state.handles if item.handle_id == access.handle_id)
            self._save_access(state, current.model_copy(update={"cancel_pending": False}))

    async def _cancel_owned(self, scope_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Detach this scope, cancelling physical work only after its final wait leaves."""
        async with self._state_lock:
            state = (
                self._namespace.load_optional(_STATE_PATH, EvaluationAgentState)
                or EvaluationAgentState()
            )
        access_handles = tuple(
            item.handle_id
            for item in state.handles
            if any(requester.scope_id == scope_id for requester in item.requesters())
        )
        claimed_handles = await self._backend.owned_handles(scope_id)
        owned = tuple(dict.fromkeys((*access_handles, *claimed_handles)))
        evaluations: list[str] = []
        for handle_id in owned:
            record = await self._backend.recorded_snapshot(handle_id)
            canceled = await self._detach_requester(
                handle_id, scope_id=scope_id, canonical_claimed=handle_id in claimed_handles
            )
            if canceled and record.state not in _TERMINAL_EVALUATION_STATES:
                if await self._backend.status(handle_id) not in _TERMINAL_EVALUATION_STATES:
                    raise ScopeClosingError(scope_id)
                evaluations.append(handle_id)
        profiler_operations = (
            await self._profiler_agents.cancel_scope(scope_id)
            if self._profiler_agents is not None
            else ()
        )
        return tuple(evaluations), profiler_operations

    async def reopen_scope(self, scope_id: str) -> None:
        """Reconcile cleanup, then open a fresh generation for a parked member."""
        if self._scopes.released(scope_id):
            await self.cancel_scope(scope_id)
        self._scopes.reopen(scope_id)

    async def scope_released(self, scope_id: str | None) -> bool:
        """Return whether admission is fenced; the root scope never is."""
        return scope_id is not None and self._scopes.released(scope_id)

    async def scope_handles(self, scope_id: str | None) -> tuple[str, ...]:
        """Return this requester's submission history, including joins and detached waits."""
        async with self._state_lock:
            state = (
                self._namespace.load_optional(_STATE_PATH, EvaluationAgentState)
                or EvaluationAgentState()
            )
        history = tuple(
            (
                min(
                    requester.submission_index
                    for requester in item.requesters()
                    if requester.scope_id == scope_id
                ),
                item.handle_id,
            )
            for item in state.handles
            if any(requester.scope_id == scope_id for requester in item.requesters())
        )
        return tuple(handle for _, handle in sorted(history, key=lambda item: item[0]))

    async def association_generation(self, handle_id: str, scope_id: str) -> int:
        """Read the current requester's generation without changing canonical ownership."""
        async with self._state_lock:
            state = self._namespace.load_optional(_STATE_PATH, EvaluationAgentState)
        access = (
            next((item for item in state.handles if item.handle_id == handle_id), None)
            if state
            else None
        )
        if access is None:
            raise EvaluationDependencyError(SettlementErrorCode.UNKNOWN_HANDLE, handle_id)
        legacy_generation = (
            (await self._backend.recorded_snapshot(handle_id)).request.owner_generation
            if not access.associations
            else 0
        )
        generation = self._scope_generation(scope_id)
        if not any(
            item.scope_id == scope_id and item.generation == generation and item.active
            for item in access.requesters(legacy_generation=legacy_generation)
        ):
            raise EvaluationDependencyError(SettlementErrorCode.UNOWNED, handle_id)
        return generation

    def _scope_generation(self, scope_id: str | None) -> int:
        return next(
            (
                item.generation
                for item in self._scopes.snapshot().scopes
                if item.scope_id == scope_id
            ),
            0,
        )

    async def _run_operations(self) -> RunOperationsReply:
        """Join durable access state with host-owned execution records."""
        async with self._state_lock:
            state = (
                self._namespace.load_optional(_STATE_PATH, EvaluationAgentState)
                or EvaluationAgentState()
            )
        observations: list[EvaluationOperationObservation] = []
        for access in state.handles[-32:]:
            snapshot = await self._backend.operation_snapshot(access.handle_id)
            observations.append(
                EvaluationOperationObservation(
                    handle_id=access.handle_id,
                    principal_ids=tuple(
                        sorted(
                            {
                                item.principal_id
                                for item in access.requesters()
                                if item.principal_id is not None
                            }
                        )
                    ),
                    scope_id=access.scope_id,
                    candidate_content_digest=access.fingerprints.candidate.value,
                    evidence_kinds=access.kinds,
                    state=snapshot.state,
                    evidence_recorded=snapshot.evidence_recorded,
                    stage_outcomes=snapshot.stage_outcomes,
                    evidence_ids=snapshot.evidence_ids,
                )
            )
        profiler_operations = (
            await self._profiler_agents.project_run() if self._profiler_agents is not None else ()
        )
        return RunOperationsReply(
            evaluations=tuple(observations),
            profiler_operations=profiler_operations,
        )

    async def _dispatch_handle(
        self,
        call: StatusCall | AwaitCall | CancelCall,
        grant: EvaluationGrant,
        access: HandleAccess,
    ) -> AgentEvaluationReply:
        """Perform one already-authorized evaluation-handle operation."""
        if isinstance(call, StatusCall):
            return StatusReply(
                handle_id=call.handle_id, status=await self._backend.status(call.handle_id)
            )
        if isinstance(call, AwaitCall):
            result = await self._backend.await_result(
                call.handle_id, min(call.timeout_s, MAX_AGENT_AWAIT_S)
            )
            if isinstance(result, EvaluationTimedOut):
                return AwaitReply(result=await self._progress(result))
            return AwaitReply(
                result=result,
                repeated_failure=(
                    await self._repeated_failure(access, grant.scope_id)
                    if isinstance(result, EvaluationFailed | EvaluationCompleted)
                    else None
                ),
            )
        if isinstance(call, CancelCall):
            associated = any(
                item.scope_id == grant.scope_id and item.principal_id == grant.principal_id
                for item in access.requesters()
            )
            if not associated:
                raise EvaluationAgentAccessError(AccessErrorCode.CANCEL_DENIED)
            await self._detach_requester(
                call.handle_id, scope_id=grant.scope_id, principal_id=grant.principal_id
            )
            record = await self._backend.recorded_snapshot(call.handle_id)
            return CanceledReply(handle_id=call.handle_id, status=record.state)
        raise AssertionError

    async def _progress(self, timed_out: EvaluationTimedOut) -> EvaluationStillRunning:
        """Report what a still-running evaluation has recorded so far.

        A timed-out wait never reports completion: a terminal state read here
        is returned as recorded, and the next await returns its result.
        """
        if timed_out.status is None:
            return EvaluationStillRunning(
                handle_id=timed_out.handle_id, state=None, next_await_s=MAX_AGENT_AWAIT_S
            )
        snapshot = await self._backend.operation_snapshot(timed_out.handle_id)
        return EvaluationStillRunning(
            handle_id=timed_out.handle_id,
            state=snapshot.state,
            current_stage=snapshot.current_stage,
            stage_outcomes=snapshot.stage_outcomes,
            next_await_s=MAX_AGENT_AWAIT_S,
        )

    async def _repeated_failure(
        self, access: HandleAccess, scope_id: str | None
    ) -> RepeatedFailure | None:
        """Describe a failure that repeats its stage's previous ones from the same workspace."""
        handles = await self.scope_handles(scope_id)
        if access.handle_id not in handles:
            return None
        snapshots = [
            await self._backend.operation_snapshot(handle_id)
            for handle_id in handles[: handles.index(access.handle_id) + 1]
        ]
        return detect_repeated_failure(snapshots)

    def _require_profiler_agents(self) -> ProfilerAgentService:
        if self._profiler_agents is None:
            raise ProfilerAgentUnavailableError
        return self._profiler_agents

    async def _dispatch_profiler(
        self,
        call: DispatchProfilerCall
        | ProfilerOperationsCall
        | ProfilerStatusCall
        | AwaitProfilerCall
        | CancelProfilerCall,
        grant: EvaluationGrant,
    ) -> AgentEvaluationReply:
        service = self._require_profiler_agents()
        if isinstance(call, DispatchProfilerCall):
            return await self._dispatch_new_profile(service, call, grant)
        if isinstance(call, ProfilerOperationsCall):
            return await service.operations(grant.principal_id)
        if isinstance(call, ProfilerStatusCall):
            return await service.status(call.operation_id, grant.principal_id, grant.scope_id)
        if isinstance(call, AwaitProfilerCall):
            return await service.await_result(
                call.operation_id,
                grant.principal_id,
                grant.scope_id,
                min(call.timeout_s, MAX_AGENT_AWAIT_S),
            )
        return await service.cancel(call.operation_id, grant.principal_id, grant.scope_id)

    async def _dispatch_new_profile(
        self,
        service: ProfilerAgentService,
        call: DispatchProfilerCall,
        grant: EvaluationGrant,
    ) -> AgentEvaluationReply:
        """Dispatch a profiler turn, or refuse it while the run stops or the scope is released."""
        if self._stopped or self._stopping():
            return RunStoppingReply()
        if await self.scope_released(grant.scope_id):
            return ScopeReleasedReply()
        dispatched = await service.dispatch(
            principal_id=grant.principal_id,
            scope_id=grant.scope_id,
            work=call.work,
            request=call.request,
            session_id=call.session_id,
            idempotency_key=call.idempotency_key,
        )
        if grant.scope_id is not None and await self.scope_released(grant.scope_id):
            # The scope was released while this dispatch was in flight.
            await self._cancel_owned(grant.scope_id)
            return ScopeReleasedReply()
        return dispatched

    async def _require_supported(self, kinds: tuple[EvidenceKind, ...]) -> None:
        """Reject kinds the executor cannot produce before any handle is claimed."""
        snapshot = await self._backend.availability(ResourceRequirements())
        unsupported = sorted(
            kind.value for kind in kinds if kind.value not in snapshot.supported_evidence_kinds
        )
        if unsupported:
            raise EvaluationAgentAccessError(
                AccessErrorCode.KIND_UNSUPPORTED, ", ".join(unsupported)
            )

    def _require_grant(self, token: str) -> EvaluationGrant:
        grant = self._grants.get(token)
        if grant is None:
            raise EvaluationAgentAccessError(AccessErrorCode.INVALID_GRANT)
        return grant

    def _authorized_availability_kinds(
        self, grant: EvaluationGrant, requested: Sequence[EvidenceKind]
    ) -> tuple[EvidenceKind, ...]:
        if grant.role is EvaluationAgentRole.JUDGE:
            raise EvaluationAgentAccessError(AccessErrorCode.JUDGE_READ_ONLY)
        observable = _AVAILABILITY_KINDS[grant.role] | self.delegated_evidence_kinds(grant.role)
        return self._authorize_kinds(grant, requested, observable)

    def _authorized_submission_kinds(
        self, grant: EvaluationGrant, requested: Sequence[EvidenceKind]
    ) -> tuple[EvidenceKind, ...]:
        if grant.role in {
            EvaluationAgentRole.PORTFOLIO_DISPATCH,
            EvaluationAgentRole.RUN_OBSERVER,
        }:
            raise EvaluationAgentAccessError(AccessErrorCode.AVAILABILITY_READ_ONLY)
        if grant.role is EvaluationAgentRole.JUDGE:
            raise EvaluationAgentAccessError(AccessErrorCode.JUDGE_READ_ONLY)
        if not requested and grant.role is EvaluationAgentRole.IMPLEMENTER:
            requested = (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK)
        return self._authorize_kinds(grant, requested, submission_evidence_kinds(grant.role))

    @staticmethod
    def _authorize_kinds(
        grant: EvaluationGrant,
        requested: Sequence[EvidenceKind],
        allowed: frozenset[EvidenceKind],
    ) -> tuple[EvidenceKind, ...]:
        kinds = tuple(requested) or tuple(sorted(allowed, key=str))
        denied = set(kinds).difference(allowed)
        if denied:
            names = ", ".join(sorted(kind.value for kind in denied))
            raise EvaluationAgentAccessError(
                AccessErrorCode.KIND_DENIED, f"{grant.role.value}: {names}"
            )
        return kinds

    def _require_evidence_reader(self, grant: EvaluationGrant) -> None:
        if grant.role not in {
            EvaluationAgentRole.IMPLEMENTER,
            EvaluationAgentRole.PROFILER,
            EvaluationAgentRole.JUDGE,
            EvaluationAgentRole.ORCHESTRATOR,
        }:
            raise EvaluationAgentAccessError(AccessErrorCode.EVIDENCE_DENIED)

    def _authorized_evidence_query(
        self, grant: EvaluationGrant, requested: Sequence[EvidenceKind]
    ) -> tuple[EvidenceKind, ...]:
        if grant.role is EvaluationAgentRole.IMPLEMENTER:
            return self._authorize_kinds(grant, requested, frozenset(EvidenceKind))
        return tuple(requested) or tuple(EvidenceKind)

    async def _remember(
        self,
        submitted: SubmittedSemanticEvaluation,
        grant: EvaluationGrant,
        kinds: tuple[EvidenceKind, ...],
    ) -> None:
        handle_id = submitted.handle_id
        record = await self._backend.recorded_snapshot(handle_id)
        async with self._state_lock:
            if grant.scope_id is not None and self._scopes.released(grant.scope_id):
                raise ScopeClosingError(grant.scope_id)
            state = (
                self._namespace.load_optional(_STATE_PATH, EvaluationAgentState)
                or EvaluationAgentState()
            )
            existing = next((item for item in state.handles if item.handle_id == handle_id), None)
            if existing is not None and (
                existing.fingerprints != submitted.fingerprints or existing.kinds != kinds
            ):
                message = f"evaluation handle {handle_id!r} resolved to different semantic work"
                raise RuntimeError(message)
            association = HandleAssociation(
                scope_id=grant.scope_id,
                generation=self._scope_generation(grant.scope_id),
                principal_id=grant.principal_id,
                submission_index=state.next_submission_index(),
            )
            canonical = (
                (
                    HandleAssociation(
                        scope_id=record.request.owner_scope,
                        generation=record.request.owner_generation,
                    ),
                )
                if (record.request.owner_scope, record.request.owner_generation)
                != (association.scope_id, association.generation)
                else ()
            )
            access = HandleAccess(
                handle_id=handle_id,
                scope_id=record.request.owner_scope,
                fingerprints=submitted.fingerprints,
                kinds=kinds,
                observers=frozenset({grant.principal_id})
                if existing is None
                else existing.observers | {grant.principal_id},
                owners=frozenset({grant.principal_id})
                if existing is None and not canonical
                else frozenset()
                if existing is None
                else existing.owners,
                associations=(canonical or (association,))
                if existing is None
                else existing.requesters(legacy_generation=record.request.owner_generation),
                cancel_pending=(
                    existing.cancel_pending and record.state not in _TERMINAL_EVALUATION_STATES
                    if existing
                    else False
                ),
            ).associate(association)
            records = tuple(
                access if item.handle_id == handle_id else item for item in state.handles
            )
            if existing is None:
                records = (*records, access)
            self._namespace.save(_STATE_PATH, EvaluationAgentState(handles=records))

    async def _require_observer(self, grant: EvaluationGrant, handle_id: str) -> HandleAccess:
        async with self._state_lock:
            state = (
                self._namespace.load_optional(_STATE_PATH, EvaluationAgentState)
                or EvaluationAgentState()
            )
        access = next((item for item in state.handles if item.handle_id == handle_id), None)
        if access is None:
            raise EvaluationAgentAccessError(AccessErrorCode.UNKNOWN_HANDLE)
        if (
            grant.role is not EvaluationAgentRole.ORCHESTRATOR
            and grant.principal_id not in access.observers
            and not any(item.scope_id == grant.scope_id for item in access.requesters())
        ):
            raise EvaluationAgentAccessError(AccessErrorCode.HANDLE_DENIED)
        return access

    async def _serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._clients.add(task)
        try:
            frame = await reader.readline()
            call = _parse_call(frame)
            reply = await self.dispatch(call)
            response = SocketSuccess(result=_REPLY_ADAPTER.dump_python(reply, mode="json"))
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-930067 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
            response = SocketFailure(error=str(exc))
        try:
            writer.write(response.model_dump_json().encode() + b"\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                writer.close()
                if task is None or not task.cancelling():
                    with suppress(BrokenPipeError, ConnectionResetError):
                        await writer.wait_closed()
            finally:
                if task is not None:
                    self._clients.discard(task)


def _parse_call(frame: bytes) -> AgentEvaluationCall:
    if not frame or len(frame) > _MAX_FRAME_BYTES or not frame.endswith(b"\n"):
        raise EvaluationAgentProtocolError
    return _CALL_ADAPTER.validate_json(frame)


__all__ = [
    "EvaluationAgentAccessError",
    "EvaluationAgentService",
    "EvaluationAgentSocketError",
    "EvaluationBackend",
]
