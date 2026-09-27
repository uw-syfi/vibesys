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
    EvidenceCall,
    EvidencePreflightCheck,
    EvidencePreflightDecision,
    EvidencePreflightResolution,
    EvidenceReply,
    HandleAccess,
    ProfilerOperationsCall,
    ProfilerStatusCall,
    RunOperationsCall,
    RunOperationsReply,
    SocketFailure,
    SocketSuccess,
    StatusCall,
    StatusReply,
    SubmitCall,
    SubmittedReply,
    SubmittedSemanticEvaluation,
)
from vs_evaluation.models import AvailabilitySnapshot, AvailabilityState, ResourceRequirements
from vs_evaluation.profiler_service import ProfilerAgentUnavailableError
from vs_project.api import validate_socket_path

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vs_evaluation.models import (
        EvaluationAwaitResult,
        EvaluationState,
        StoredEvaluation,
    )
    from vs_evaluation.profiler_service import ProfilerAgentService
    from vs_project.api import StateNamespace

_STATE_PATH = "agent-evaluation-access.json"
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
            AccessErrorCode.EVIDENCE_DENIED: "accepted evidence is unavailable to this role",
            AccessErrorCode.UNKNOWN_HANDLE: "unknown evaluation handle",
            AccessErrorCode.HANDLE_DENIED: "evaluation handle is not visible to this principal",
            AccessErrorCode.CANCEL_DENIED: "only an owner or orchestrator may cancel evaluation",
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


class EvaluationAgentService:
    """Own authorization, durable handle access, and a private Unix socket."""

    def __init__(
        self,
        backend: EvaluationBackend,
        namespace: StateNamespace,
        socket_path: Path,
        profiler_agents: ProfilerAgentService | None = None,
    ) -> None:
        """Bind the semantic coordinator, project state, and private socket."""
        self._backend = backend
        self._namespace = namespace
        self._socket_path = validate_socket_path(socket_path)
        self._profiler_agents = profiler_agents
        self._grants: dict[str, EvaluationGrant] = {}
        self._scoped_grants: dict[
            tuple[str, EvaluationAgentRole, str | None, bool], EvaluationGrant
        ] = {}
        self._state_lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._clients: set[asyncio.Task[None]] = set()

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
    ) -> EvaluationGrant:
        """Return the stable process-local capability for one principal and scope."""
        key = (principal_id, role, scope_id, run_observer)
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
        )
        self._grants[grant.token] = grant
        self._scoped_grants[key] = grant
        return grant

    async def start(self) -> None:
        """Start accepting strict one-request JSONL connections."""
        if self._server is not None:
            return
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
        """Reconcile and cancel every evaluation submitted through this service."""
        async with self._state_lock:
            state = (
                self._namespace.load_optional(_STATE_PATH, EvaluationAgentState)
                or EvaluationAgentState()
            )
        cancellations = await asyncio.gather(
            *(self._backend.cancel(item.handle_id) for item in state.handles),
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
            kinds = self._authorized_submission_kinds(grant, call.evidence_kinds)
            submitted = await self._backend.submit_evidence(grant.scope_id, kinds)
            await self._remember(submitted, grant, kinds)
            return SubmittedReply(handle_id=submitted.handle_id)
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
        access = await self._require_observer(grant, call.handle_id)
        return await self._dispatch_handle(call, grant, access)

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
                    principal_ids=tuple(sorted(access.owners)),
                    scope_id=access.scope_id,
                    candidate_content_digest=access.fingerprints.candidate.value,
                    evidence_kinds=access.kinds,
                    state=snapshot.state,
                    accepted_result=snapshot.accepted_result,
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
            return AwaitReply(
                result=await self._backend.await_result(call.handle_id, call.timeout_s)
            )
        if isinstance(call, CancelCall):
            if (
                grant.role is not EvaluationAgentRole.ORCHESTRATOR
                and grant.principal_id not in access.owners
            ):
                raise EvaluationAgentAccessError(AccessErrorCode.CANCEL_DENIED)
            record = await self._backend.cancel(call.handle_id)
            return CanceledReply(handle_id=call.handle_id, status=record.state)
        raise AssertionError

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
            return await service.dispatch(
                principal_id=grant.principal_id,
                scope_id=grant.scope_id,
                work=call.work,
                request=call.request,
                session_id=call.session_id,
                idempotency_key=call.idempotency_key,
            )
        if isinstance(call, ProfilerOperationsCall):
            return await service.operations(grant.principal_id)
        if isinstance(call, ProfilerStatusCall):
            return await service.status(call.operation_id, grant.principal_id, grant.scope_id)
        if isinstance(call, AwaitProfilerCall):
            return await service.await_result(
                call.operation_id,
                grant.principal_id,
                grant.scope_id,
                call.timeout_s,
            )
        return await service.cancel(call.operation_id, grant.principal_id, grant.scope_id)

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
        async with self._state_lock:
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
            access = HandleAccess(
                handle_id=handle_id,
                scope_id=grant.scope_id,
                fingerprints=submitted.fingerprints,
                kinds=kinds,
                observers=frozenset({grant.principal_id})
                if existing is None
                else existing.observers | {grant.principal_id},
                owners=frozenset({grant.principal_id})
                if existing is None
                else existing.owners | {grant.principal_id},
            )
            records = (*(item for item in state.handles if item.handle_id != handle_id), access)
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
            and grant.scope_id != access.scope_id
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
