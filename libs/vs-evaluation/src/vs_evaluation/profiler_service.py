"""Typed profiler-agent conversations over the generic async operation lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_validator

from vs_async_ops.api import (
    OperationCompleted,
    OperationCoordinator,
    OperationHandle,
    OperationLifecycleEvent,
    OperationPolicy,
    OperationRequest,
    OperationState,
)
from vs_evaluation.profiler_models import (
    MAX_PROFILER_REQUEST_CHARS,
    CompletedProfilerOperation,
    InFlightProfilerOperation,
    ProfilerAgentResult,
    ProfilerAwaitReply,
    ProfilerCanceledReply,
    ProfilerCandidateProjection,
    ProfilerDispatchedReply,
    ProfilerLifecycleEvent,
    ProfilerOperation,
    ProfilerOperationLifecycle,
    ProfilerOperationReference,
    ProfilerOperationResult,
    ProfilerOperationsReply,
    ProfilerOperationState,
    ProfilerRunObservation,
    ProfilerStatusReply,
    ProfilerWorkKey,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from vs_async_ops.api import OperationWaiter
    from vs_evaluation.agent_evidence import TrustedEvidence
    from vs_project.api import StateNamespace

_STATE_DIRECTORY = "profiler-agent-operations"
_INDEX_PATH = f"{_STATE_DIRECTORY}/index.json"
_RESULT_ADAPTER = TypeAdapter(ProfilerOperationResult)
MAX_LIVE_PROFILER_OPERATIONS = 256
PROFILER_TERMINAL_RETENTION = 128
_MAX_OPERATION_REFERENCES = 16


class ProfilerAgentUnavailableError(RuntimeError):
    """The run environment did not provision a profiler agent."""

    def __init__(self) -> None:
        """Build the fixed missing-provision diagnostic."""
        super().__init__("no profiler agent is provisioned")


class ProfilerAgentAccessError(PermissionError):
    """A principal attempted to observe or resume another conversation."""

    @classmethod
    def unknown_session(cls) -> ProfilerAgentAccessError:
        """Reject a conversation ID absent from durable state."""
        return cls("unknown profiler session")

    @classmethod
    def wrong_scope(cls, subject: str) -> ProfilerAgentAccessError:
        """Reject cross-principal or cross-scope access."""
        return cls(f"profiler {subject} belongs to another scope")

    @classmethod
    def provision_changed(cls) -> ProfilerAgentAccessError:
        """Reject resuming a session under different agent configuration."""
        return cls("profiler session uses a different provision")


class ProfilerAgentStateError(RuntimeError):
    """Durable profiler state violated its revision contract."""

    def __init__(self, operation_id: str, reason: str) -> None:
        super().__init__(f"profiler operation {operation_id!r} {reason}")


class InvalidCandidateSnapshotError(ValueError):
    """The host failed to provide an exact candidate identity."""

    def __init__(self) -> None:
        """Build the fixed invalid-snapshot diagnostic."""
        super().__init__("candidate snapshot identity must be nonblank")


class ProfilerIdempotencyConflictError(ValueError):
    """A caller reused a profiler retry key for different work."""

    def __init__(self) -> None:
        """Build the fixed changed-request diagnostic."""
        super().__init__("profiler idempotency key was reused with a different request")


class ProfilerAgentCapacityError(RuntimeError):
    """The bounded durable queue cannot accept another profiler turn."""

    def __init__(self) -> None:
        """Build the fixed durable queue capacity diagnostic."""
        super().__init__("profiler operation queue reached its durable capacity")


class ProfilerTurnProvision(Protocol):
    """Environment-owned profiler agent, including its prompt, skills, and tools."""

    @property
    def identity(self) -> str:
        """Stable provision identity for observability."""
        ...

    async def run_turn(
        self,
        *,
        session_id: str,
        operation_id: str,
        request: str,
        scope_id: str | None,
        candidate_snapshot_id: str,
    ) -> ProfilerAgentResult:
        """Start or resume the named profiler conversation."""
        ...

    async def cancel(self, operation_id: str) -> None:
        """Cancel one in-flight profiler turn idempotently."""
        ...

    async def cancel_scope(self, scope_id: str) -> None:
        """Retire provision resources owned by a discarded candidate scope."""
        ...


class _ProfilerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    principal_id: str
    scope_id: str | None
    request: str = Field(min_length=1, max_length=MAX_PROFILER_REQUEST_CHARS)
    work: ProfilerWorkKey
    candidate_snapshot_id: str
    provision_identity: str
    idempotency_key: str | None = None


class _StoredOperationIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_ids: tuple[str, ...] = ()

    @field_validator("operation_ids")
    @classmethod
    def _safe_unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("profiler operation IDs must be unique")  # noqa: TRY003  # lint-waiver: LW-930064 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        if any(not item or not item.isascii() or not item.isalnum() for item in value):
            raise ValueError("profiler operation IDs must be ASCII alphanumeric")  # noqa: TRY003  # lint-waiver: LW-930065 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return value


class _NamespaceOperationStore:
    def __init__(self, namespace: StateNamespace) -> None:
        self._namespace = namespace
        self._lock = asyncio.Lock()

    async def create(self, record: OperationHandle) -> None:
        async with self._lock:
            index = self._load_index()
            operation_id = record.request.operation_id
            if operation_id in index.operation_ids:
                raise ProfilerAgentStateError(operation_id, "already exists")
            live = sum(
                not self._namespace.load(self._record_path(item), OperationHandle).state.terminal
                for item in index.operation_ids
            )
            if live >= MAX_LIVE_PROFILER_OPERATIONS:
                raise ProfilerAgentCapacityError
            self._namespace.save(self._record_path(operation_id), record)
            self._save_index((*index.operation_ids, operation_id))

    async def get(self, operation_id: str) -> OperationHandle | None:
        async with self._lock:
            index = self._load_index()
            if operation_id not in index.operation_ids:
                return None
            return self._namespace.load(self._record_path(operation_id), OperationHandle)

    async def replace(self, record: OperationHandle, *, expected_revision: int) -> None:
        async with self._lock:
            operation_id = record.request.operation_id
            index = self._load_index()
            current = (
                self._namespace.load(self._record_path(operation_id), OperationHandle)
                if operation_id in index.operation_ids
                else None
            )
            if current is None or current.revision != expected_revision:
                raise ProfilerAgentStateError(operation_id, "revision conflict")
            self._namespace.save(self._record_path(operation_id), record)
            if record.state.terminal:
                self._compact_terminal_records(index)

    async def records(self) -> tuple[OperationHandle, ...]:
        async with self._lock:
            return tuple(
                self._namespace.load(self._record_path(operation_id), OperationHandle)
                for operation_id in self._load_index().operation_ids
            )

    def _load_index(self) -> _StoredOperationIndex:
        return (
            self._namespace.load_optional(_INDEX_PATH, _StoredOperationIndex)
            or _StoredOperationIndex()
        )

    def _save_index(self, operation_ids: tuple[str, ...]) -> None:
        self._namespace.save(_INDEX_PATH, _StoredOperationIndex(operation_ids=operation_ids))

    def _compact_terminal_records(self, index: _StoredOperationIndex) -> None:
        """Retain all live work and a deterministic recent terminal window.

        Session ownership and idempotency remain exact while an operation is
        retained. Once an old terminal record leaves the window, its operation
        and session IDs are intentionally no longer resumable or queryable.
        """
        records = tuple(
            self._namespace.load(self._record_path(operation_id), OperationHandle)
            for operation_id in index.operation_ids
        )
        terminal_ids = tuple(
            record.request.operation_id for record in records if record.state.terminal
        )
        retired = set(terminal_ids[:-PROFILER_TERMINAL_RETENTION])
        if not retired:
            return
        retained = tuple(
            operation_id for operation_id in index.operation_ids if operation_id not in retired
        )
        self._save_index(retained)
        for operation_id in retired:
            self._namespace.delete(self._record_path(operation_id))

    @staticmethod
    def _record_path(operation_id: str) -> str:
        return f"{_STATE_DIRECTORY}/{operation_id}.json"


class _ProfilerRunner:
    def __init__(
        self,
        provision: ProfilerTurnProvision,
        resolve_evidence: Callable[
            [str, str | None, str, tuple[str, ...]], Awaitable[tuple[TrustedEvidence, ...]]
        ],
    ) -> None:
        self._provision = provision
        self._resolve_evidence = resolve_evidence

    async def run(self, request: OperationRequest) -> JsonValue:
        payload = _ProfilerPayload.model_validate(request.payload)
        result = await self._provision.run_turn(
            session_id=request.concurrency_key,
            operation_id=request.operation_id,
            request=payload.request,
            scope_id=payload.scope_id,
            candidate_snapshot_id=payload.candidate_snapshot_id,
        )
        evidence = await self._resolve_evidence(
            payload.principal_id,
            payload.scope_id,
            payload.candidate_snapshot_id,
            result.evidence_ids,
        )
        return ProfilerOperationResult(
            report=result,
            trusted_evidence=evidence,
        ).model_dump(mode="json")

    async def cancel(self, operation_id: str) -> None:
        await self._provision.cancel(operation_id)


_STATE_MAP = {
    OperationState.QUEUED: ProfilerOperationState.QUEUED,
    OperationState.RUNNING: ProfilerOperationState.RUNNING,
    OperationState.SUCCEEDED: ProfilerOperationState.COMPLETED,
    OperationState.FAILED: ProfilerOperationState.FAILED,
    OperationState.CANCELED: ProfilerOperationState.CANCELED,
    OperationState.INTERRUPTED: ProfilerOperationState.INTERRUPTED,
}


@dataclass(frozen=True, slots=True)
class ProfilerAgentServiceHooks:
    """Injected host effects used by the profiler conversation service."""

    candidate_snapshot: Callable[[str | None], Awaitable[str]]
    resolve_evidence: Callable[
        [str, str | None, str, tuple[str, ...]], Awaitable[tuple[TrustedEvidence, ...]]
    ]
    waiter: OperationWaiter | None = None
    events: Callable[[ProfilerLifecycleEvent], None] | None = None


@dataclass(frozen=True, slots=True)
class _IdempotencyLookup:
    principal_id: str
    scope_id: str | None
    request: str
    work: ProfilerWorkKey
    session_id: str | None
    key: str
    provision_identity: str


@dataclass(slots=True)
class _DispatchLockEntry:
    lock: asyncio.Lock
    users: int = 0


class ProfilerAgentService:
    """Authorize and coordinate asynchronous profiler-agent conversations."""

    def __init__(
        self,
        provision: ProfilerTurnProvision | None,
        namespace: StateNamespace,
        hooks: ProfilerAgentServiceHooks,
        policy: OperationPolicy | None = None,
    ) -> None:
        """Bind one environment provision and project-owned state."""
        self._provision = provision
        self._events = hooks.events
        self._candidate_snapshot = hooks.candidate_snapshot
        self._start_lock = asyncio.Lock()
        self._dispatch_locks: dict[tuple[str, str | None, str], _DispatchLockEntry] = {}
        self._started = False
        self._event_context: dict[str, tuple[str | None, str]] = {}
        store = _NamespaceOperationStore(namespace)
        self._store = store
        self._coordinator = OperationCoordinator(
            _ProfilerRunner(provision, hooks.resolve_evidence)
            if provision is not None
            else _UnavailableRunner(),
            store,
            waiter=hooks.waiter,
            events=self._on_event,
            policy=policy,
        )

    async def dispatch(  # noqa: PLR0913  # lint-waiver: LW-092701 [PLR0913]; keeping authorization identity, semantic work, and conversation routing explicit avoids a duplicate mutable dispatch container that callers would have to unpack.
        self,
        *,
        principal_id: str,
        scope_id: str | None,
        request: str,
        work: ProfilerWorkKey,
        session_id: str | None,
        idempotency_key: str | None = None,
        candidate_snapshot_id: str | None = None,
    ) -> ProfilerDispatchedReply:
        """Create or resume a conversation and return before its turn completes.

        The candidate is ``scope_id``'s live workspace, snapshotted now, unless
        ``candidate_snapshot_id`` names an exact revision to profile instead.
        """
        if self._provision is None:
            raise ProfilerAgentUnavailableError
        await self._ensure_started()
        if idempotency_key is not None:
            deduplication_scope = (principal_id, scope_id, idempotency_key)
            async with self._deduplication_lock(deduplication_scope):
                existing = await self._idempotent_operation(
                    _IdempotencyLookup(
                        principal_id=principal_id,
                        scope_id=scope_id,
                        request=request,
                        work=work,
                        session_id=session_id,
                        key=idempotency_key,
                        provision_identity=self._provision.identity,
                    )
                )
                if existing is not None:
                    return existing
                return await self._dispatch_new(
                    principal_id=principal_id,
                    scope_id=scope_id,
                    request=request,
                    work=work,
                    session_id=session_id,
                    idempotency_key=idempotency_key,
                    candidate_snapshot_id=candidate_snapshot_id,
                )
        return await self._dispatch_new(
            principal_id=principal_id,
            scope_id=scope_id,
            request=request,
            work=work,
            session_id=session_id,
            idempotency_key=None,
            candidate_snapshot_id=candidate_snapshot_id,
        )

    @asynccontextmanager
    async def _deduplication_lock(self, key: tuple[str, str | None, str]) -> AsyncIterator[None]:
        """Serialize one retry identity and retire its lock after all waiters leave."""
        entry = self._dispatch_locks.setdefault(key, _DispatchLockEntry(asyncio.Lock()))
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._dispatch_locks.get(key) is entry:
                self._dispatch_locks.pop(key)

    async def _dispatch_new(  # noqa: PLR0913  # lint-waiver: LW-092702 [PLR0913]; this private boundary mirrors the validated public fields so durable submission cannot silently omit identity or semantic equivalence data.
        self,
        *,
        principal_id: str,
        scope_id: str | None,
        request: str,
        work: ProfilerWorkKey,
        session_id: str | None,
        idempotency_key: str | None,
        candidate_snapshot_id: str | None,
    ) -> ProfilerDispatchedReply:
        """Snapshot and durably submit one operation after deduplication."""
        if self._provision is None:
            raise ProfilerAgentUnavailableError
        resolved_session = session_id or uuid.uuid4().hex
        if session_id is not None:
            await self._require_session_owner(
                session_id,
                principal_id,
                self._provision.identity,
            )
        operation_id = uuid.uuid4().hex
        if candidate_snapshot_id is None:
            candidate_snapshot_id = await self._candidate_snapshot(scope_id)
        if not candidate_snapshot_id.strip():
            raise InvalidCandidateSnapshotError
        self._event_context[operation_id] = (
            scope_id,
            hashlib.sha256(request.encode()).hexdigest(),
        )
        await self._coordinator.submit(
            OperationRequest(
                operation_id=operation_id,
                concurrency_key=resolved_session,
                payload=_ProfilerPayload(
                    principal_id=principal_id,
                    scope_id=scope_id,
                    request=request,
                    work=work,
                    candidate_snapshot_id=candidate_snapshot_id,
                    provision_identity=self._provision.identity,
                    idempotency_key=idempotency_key,
                ).model_dump(mode="json"),
            )
        )
        return ProfilerDispatchedReply(
            session_id=resolved_session,
            operation_id=operation_id,
        )

    async def _idempotent_operation(
        self, lookup: _IdempotencyLookup
    ) -> ProfilerDispatchedReply | None:
        """Return the durable operation for an exact retry key."""
        for record in await self._coordinator.records():
            payload = _ProfilerPayload.model_validate(record.request.payload)
            if (
                payload.principal_id,
                payload.scope_id,
                payload.idempotency_key,
            ) != (lookup.principal_id, lookup.scope_id, lookup.key):
                continue
            if (
                payload.request != lookup.request
                or payload.work != lookup.work
                or payload.provision_identity != lookup.provision_identity
                or (
                    lookup.session_id is not None
                    and record.request.concurrency_key != lookup.session_id
                )
            ):
                raise ProfilerIdempotencyConflictError
            return ProfilerDispatchedReply(
                session_id=record.request.concurrency_key,
                operation_id=record.request.operation_id,
            )
        return None

    async def status(
        self, operation_id: str, principal_id: str, scope_id: str | None
    ) -> ProfilerStatusReply:
        """Return an operation visible to ``principal_id``."""
        del scope_id  # Observation authority follows the durable principal, not a worktree.
        await self._ensure_started()
        record = await self._coordinator.status(operation_id)
        self._require_owner(record, principal_id)
        return ProfilerStatusReply(operation=_operation(record, include_result=True))

    async def operations(self, principal_id: str) -> ProfilerOperationsReply:
        """Return recent durable profiler turns owned by one logical implementer."""
        await self._ensure_started()
        owned = tuple(
            record
            for record in await self._coordinator.records()
            if _ProfilerPayload.model_validate(record.request.payload).principal_id == principal_id
        )
        return ProfilerOperationsReply(
            operations=tuple(
                _operation_reference(record) for record in owned[-_MAX_OPERATION_REFERENCES:]
            )
        )

    async def project_principal(self, principal_id: str) -> tuple[ProfilerOperationLifecycle, ...]:
        """Return concise lifecycle facts for framework policy, without report narratives."""
        await self._ensure_started()
        owned = tuple(
            record
            for record in await self._coordinator.records()
            if _ProfilerPayload.model_validate(record.request.payload).principal_id == principal_id
        )
        return tuple(_operation_lifecycle(record) for record in owned[-8:])

    async def project_run(self) -> tuple[ProfilerRunObservation, ...]:
        """Return recent host-owned profiler facts across logical implementers."""
        await self._ensure_started()
        return tuple(
            _run_observation(record) for record in (await self._coordinator.records())[-32:]
        )

    async def await_result(
        self,
        operation_id: str,
        principal_id: str,
        scope_id: str | None,
        timeout_s: float,
    ) -> ProfilerAwaitReply:
        """Wait at most ``timeout_s`` without canceling on timeout."""
        del scope_id  # Observation authority follows the durable principal, not a worktree.
        await self._ensure_started()
        self._require_owner(await self._coordinator.status(operation_id), principal_id)
        observed = await self._coordinator.await_result(operation_id, timeout_s)
        return ProfilerAwaitReply(
            timed_out=not isinstance(observed, OperationCompleted),
            operation=_operation(observed.record, include_result=True),
        )

    async def cancel(
        self, operation_id: str, principal_id: str, scope_id: str | None
    ) -> ProfilerCanceledReply:
        """Cancel one operation owned by ``principal_id``."""
        del scope_id  # Cancellation authority follows the durable principal, not a worktree.
        await self._ensure_started()
        self._require_owner(await self._coordinator.status(operation_id), principal_id)
        return ProfilerCanceledReply(
            operation=_operation(await self._coordinator.cancel(operation_id), include_result=True)
        )

    async def cancel_scope(self, scope_id: str) -> None:
        """Cancel all nonterminal profiler turns owned by a discarded scope."""
        await self._ensure_started()
        for record in await self._coordinator.records():
            payload = _ProfilerPayload.model_validate(record.request.payload)
            if payload.scope_id == scope_id and record.state not in {
                OperationState.SUCCEEDED,
                OperationState.FAILED,
                OperationState.CANCELED,
                OperationState.INTERRUPTED,
            }:
                await self._coordinator.cancel(record.request.operation_id)
        if self._provision is not None:
            await self._provision.cancel_scope(scope_id)

    async def project_candidate(self, candidate_snapshot_id: str) -> ProfilerCandidateProjection:
        """Return bounded completed and active work for one exact candidate.

        This framework-only projection deliberately omits principals, scopes,
        requests, failures, and backend details.  Optimization policy can
        consume validated results or suppress equivalent duplicate work
        without gaining access to another agent's capability.
        """
        if not candidate_snapshot_id.strip():
            raise InvalidCandidateSnapshotError
        await self._ensure_started()
        matching = []
        for record in await self._coordinator.records():
            payload = _ProfilerPayload.model_validate(record.request.payload)
            if payload.candidate_snapshot_id == candidate_snapshot_id:
                matching.append(record)
        in_flight = tuple(
            InFlightProfilerOperation(
                operation_id=record.request.operation_id,
                work=_ProfilerPayload.model_validate(record.request.payload).work,
            )
            for record in matching
            if record.state in {OperationState.QUEUED, OperationState.RUNNING}
        )[-32:]
        completed = tuple(
            CompletedProfilerOperation(
                operation_id=record.request.operation_id,
                work=_ProfilerPayload.model_validate(record.request.payload).work,
                result=_RESULT_ADAPTER.validate_python(record.result),
            )
            for record in matching
            if record.state is OperationState.SUCCEEDED
        )[-32:]
        return ProfilerCandidateProjection(
            candidate_snapshot_id=candidate_snapshot_id,
            in_flight=in_flight,
            completed=completed,
        )

    async def close(self) -> None:
        """Interrupt operations owned by this process."""
        await self._ensure_started()
        await self._coordinator.close()

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            for record in await self._store.records():
                payload = _ProfilerPayload.model_validate(record.request.payload)
                self._event_context[record.request.operation_id] = (
                    payload.scope_id,
                    hashlib.sha256(payload.request.encode()).hexdigest(),
                )
            await self._coordinator.start()
            self._started = True

    async def _require_session_owner(
        self,
        session_id: str,
        principal_id: str,
        provision_identity: str,
    ) -> None:
        matches = [
            item
            for item in await self._coordinator.records()
            if item.request.concurrency_key == session_id
        ]
        if not matches:
            raise ProfilerAgentAccessError.unknown_session()
        payload = _ProfilerPayload.model_validate(matches[0].request.payload)
        if payload.principal_id != principal_id:
            raise ProfilerAgentAccessError.wrong_scope("session")
        if payload.provision_identity != provision_identity:
            raise ProfilerAgentAccessError.provision_changed()

    @staticmethod
    def _require_owner(record: OperationHandle, principal_id: str) -> None:
        payload = _ProfilerPayload.model_validate(record.request.payload)
        if payload.principal_id != principal_id:
            raise ProfilerAgentAccessError.wrong_scope("operation")

    def _on_event(self, event: OperationLifecycleEvent) -> None:
        if self._events is not None:
            scope_id, request_digest = self._event_context[event.operation_id]
            self._events(
                ProfilerLifecycleEvent(
                    operation_id=event.operation_id,
                    session_id=event.concurrency_key,
                    scope_id=scope_id,
                    request_digest=request_digest,
                    state=_STATE_MAP[event.state],
                )
            )
        if event.state.terminal:
            self._event_context.pop(event.operation_id, None)


class _UnavailableRunner:
    async def run(self, request: OperationRequest) -> JsonValue:
        del request
        raise ProfilerAgentUnavailableError

    async def cancel(self, operation_id: str) -> None:
        del operation_id


def _operation(record: OperationHandle, *, include_result: bool) -> ProfilerOperation:
    payload = _ProfilerPayload.model_validate(record.request.payload)
    result = (
        _RESULT_ADAPTER.validate_python(record.result)
        if include_result and record.state is OperationState.SUCCEEDED
        else None
    )
    return ProfilerOperation(
        operation_id=record.request.operation_id,
        session_id=record.request.concurrency_key,
        principal_id=payload.principal_id,
        scope_id=payload.scope_id,
        request=payload.request,
        work=payload.work,
        request_digest=hashlib.sha256(payload.request.encode()).hexdigest(),
        candidate_snapshot_id=payload.candidate_snapshot_id,
        provision_identity=payload.provision_identity,
        state=_STATE_MAP[record.state],
        result=result,
        error=record.failure,
    )


def _operation_reference(record: OperationHandle) -> ProfilerOperationReference:
    """Project enough identity to recover a profiler conversation without large results."""
    payload = _ProfilerPayload.model_validate(record.request.payload)
    return ProfilerOperationReference(
        operation_id=record.request.operation_id,
        session_id=record.request.concurrency_key,
        request=payload.request,
        work=payload.work,
        candidate_snapshot_id=payload.candidate_snapshot_id,
        state=_STATE_MAP[record.state],
    )


def _operation_lifecycle(record: OperationHandle) -> ProfilerOperationLifecycle:
    """Project operation identity and trusted terminal metadata only."""
    payload = _ProfilerPayload.model_validate(record.request.payload)
    result = (
        _RESULT_ADAPTER.validate_python(record.result)
        if record.state is OperationState.SUCCEEDED
        else None
    )
    return ProfilerOperationLifecycle(
        operation_id=record.request.operation_id,
        session_id=record.request.concurrency_key,
        request=payload.request,
        work=payload.work,
        candidate_snapshot_id=payload.candidate_snapshot_id,
        state=_STATE_MAP[record.state],
        outcome=result.report.outcome if result is not None else None,
        trusted_evidence_ids=(result.report.evidence_ids if result is not None else ()),
    )


def _run_observation(record: OperationHandle) -> ProfilerRunObservation:
    """Project one durable profiler record for trusted run-wide observation."""
    operation = _operation(record, include_result=True)
    result = operation.result
    return ProfilerRunObservation(
        operation_id=operation.operation_id,
        session_id=operation.session_id,
        principal_id=operation.principal_id,
        scope_id=operation.scope_id,
        request=operation.request,
        work=operation.work,
        candidate_snapshot_id=operation.candidate_snapshot_id,
        state=operation.state,
        evidence_recorded=result is not None and bool(result.trusted_evidence),
        outcome=result.report.outcome if result is not None else None,
        trusted_evidence_ids=(result.report.evidence_ids if result is not None else ()),
    )


__all__ = [
    "MAX_LIVE_PROFILER_OPERATIONS",
    "PROFILER_TERMINAL_RETENTION",
    "ProfilerAgentAccessError",
    "ProfilerAgentCapacityError",
    "ProfilerAgentService",
    "ProfilerAgentServiceHooks",
    "ProfilerAgentUnavailableError",
    "ProfilerIdempotencyConflictError",
    "ProfilerTurnProvision",
]
