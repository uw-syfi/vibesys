"""Workspace request executors: core workspace requests over ``RuntimeWorkspaces``.

Each request is executed at most once per canonical request identity. A durable
receipt store records an intent before the side effect and the typed result
after it. Re-executing a request with a stored result returns that result;
re-executing one with only an intent inspects the workspace and either
reattaches, repeats a naturally idempotent effect, or returns an explicit
Unknown observation. No path fabricates a second snapshot, restore or
candidate.

Revision identity: ``RevisionRef.revision_id`` is the Git commit and
``RevisionRef.digest`` is ``git-commit:<commit>``. Core defines no stronger
digest today (see the lane handoff), so a pair is accepted only when the digest
matches the commit and the commit exists in this run's repository.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from vs_core.api import (
    AdoptionObserved,
    AdoptRevision,
    AttemptRef,
    ContractError,
    DiscardWorkspace,
    EnsureWorkspace,
    EventId,
    Observation,
    ObservationStatus,
    RequestId,
    RequestObserved,
    ResourceId,
    RestoreRevision,
    RetainRevision,
    RevisionId,
    RevisionRef,
    RunInvocationCheckpointObserved,
    SetupFailureKind,
    SnapshotAndRetain,
    SnapshotAndRetainRun,
    TrustedBaseline,
    VerifyAdoption,
    WorkspaceMode,
    WorkspaceObserved,
)
from vs_runtime._core_requests import (
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    ExecutorRefusal,
    ExecutorRole,
    OwnerEvent,
)
from vs_runtime._workspaces import RuntimeCandidateWorkspace
from vs_runtime.contracts import RuntimeContractError, WorkspaceRestoreError

if TYPE_CHECKING:
    from vs_core.api import Request
    from vs_runtime._workspaces import RuntimeWorkspace, RuntimeWorkspaces

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_PREFIX = "git-commit:"


def revision_ref(commit: str) -> RevisionRef:
    """Return the canonical core reference of one Git commit."""
    return RevisionRef(revision_id=RevisionId(root=commit), digest=f"{_DIGEST_PREFIX}{commit}")


def _commit_of(ref: RevisionRef) -> str | None:
    """Return the commit named by a reference, or ``None`` if it is not canonical."""
    commit = ref.revision_id.root
    if _COMMIT.match(commit) is None or ref.digest != f"{_DIGEST_PREFIX}{commit}":
        return None
    return commit


class ReceiptPhase(StrEnum):
    """How far one request's side effect progressed."""

    BEGUN = "begun"
    DONE = "done"


class ExecutionRecord(BaseModel):
    """Durable intent or result of one request, bound to its payload digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    payload_digest: str
    phase: ReceiptPhase
    result: ExecutionResult | None = None


class AttemptBinding(BaseModel):
    """The workspace one attempt owns, fixed by its first accepted ensure."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    ensure_request: RequestId
    mode: WorkspaceMode
    base: RevisionRef
    resource_id: ResourceId
    workspace_id: str | None
    path: str


class WorkspaceReceipts(Protocol):
    """Durable request receipts and attempt bindings; writes are atomic."""

    def load_execution(self, request_id: RequestId) -> ExecutionRecord | None: ...

    def save_execution(self, request_id: RequestId, record: ExecutionRecord) -> None: ...

    def load_binding(self, attempt: AttemptRef) -> AttemptBinding | None: ...

    def save_binding(self, attempt: AttemptRef, binding: AttemptBinding) -> None: ...


def _attempt_key(attempt: AttemptRef) -> str:
    return f"{attempt.attempt_id.root}:{attempt.generation}"


def _member_id(attempt: AttemptRef) -> str:
    """Stable member identity: one candidate path per attempt generation."""
    return "attempt-" + hashlib.sha256(_attempt_key(attempt).encode()).hexdigest()[:24]


class DirectoryWorkspaceReceipts:
    """File-backed receipts: one atomically replaced JSON file per identity."""

    def __init__(self, directory: Path) -> None:
        self._executions = directory / "executions"
        self._bindings = directory / "bindings"
        self._executions.mkdir(parents=True, exist_ok=True)
        self._bindings.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _name(identity: str) -> str:
        return hashlib.sha256(identity.encode()).hexdigest() + ".json"

    @staticmethod
    def _write(path: Path, text: str) -> None:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def load_execution(self, request_id: RequestId) -> ExecutionRecord | None:
        path = self._executions / self._name(request_id.root)
        if not path.is_file():
            return None
        return ExecutionRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def save_execution(self, request_id: RequestId, record: ExecutionRecord) -> None:
        self._write(self._executions / self._name(request_id.root), record.model_dump_json())

    def load_binding(self, attempt: AttemptRef) -> AttemptBinding | None:
        path = self._bindings / self._name(_attempt_key(attempt))
        if not path.is_file():
            return None
        return AttemptBinding.model_validate_json(path.read_text(encoding="utf-8"))

    def save_binding(self, attempt: AttemptRef, binding: AttemptBinding) -> None:
        self._write(self._bindings / self._name(_attempt_key(attempt)), binding.model_dump_json())


@dataclass(frozen=True)
class _Facts:
    """Interface facts of one request, before translation to core events."""

    status: ObservationStatus
    terminal: bool = True
    accepted: bool = False
    released: bool = False
    children_complete: bool = False
    resource_id: ResourceId | None = None
    revision: RevisionRef | None = None
    diagnostic: str = ""
    setup_failure: SetupFailureKind = SetupFailureKind.UNKNOWN


def _rejected(diagnostic: str, *, resource_id: ResourceId | None = None) -> _Facts:
    return _Facts(
        ObservationStatus.REJECTED,
        resource_id=resource_id,
        diagnostic=diagnostic,
        setup_failure=SetupFailureKind.PERMANENT,
    )


def _unknown(diagnostic: str, *, resource_id: ResourceId | None = None) -> _Facts:
    return _Facts(
        ObservationStatus.UNKNOWN, terminal=False, resource_id=resource_id, diagnostic=diagnostic
    )


type _Handled = (
    EnsureWorkspace | RestoreRevision | SnapshotAndRetain | RetainRevision | DiscardWorkspace
)
_HANDLED = (EnsureWorkspace, RestoreRevision, SnapshotAndRetain, RetainRevision, DiscardWorkspace)


class RuntimeWorkspaceRequests:
    """Translate workspace requests into ``RuntimeWorkspaces`` calls, once each.

    Adoption requests and attempt-scope closure are not supported by this
    executor and return a typed refusal.
    """

    def __init__(self, workspaces: RuntimeWorkspaces, receipts: WorkspaceReceipts) -> None:
        self._workspaces = workspaces
        self._receipts = receipts
        self._lock = asyncio.Lock()
        self._epochs: dict[str, int] = {}

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionOutcome:
        """Execute, replay or inspect one request under its canonical identity."""
        request_id = request.request_id
        if request_id is None:
            message = "request_id: execution requires a canonical identity"
            raise ValueError(message)
        if not isinstance(
            request, (*_HANDLED, SnapshotAndRetainRun, AdoptRevision, VerifyAdoption)
        ):
            return self._refusal(request_id, f"{request.kind} is not executed by this role yet")
        owner = f"{request.scope.owner.kind}:{request.scope.owner.root}:{request.scope.generation}"
        async with self._lock:
            if context.fence.epoch < self._epochs.get(owner, 0):
                raise ContractError(("fence", "epoch"), "stale execution host")
            self._epochs[owner] = context.fence.epoch
            stored = self._receipts.load_execution(request_id)
            if stored is not None and stored.payload_digest != context.payload_digest:
                return self._refusal(request_id, "same request identity with another payload")
            if stored is not None and stored.result is not None:
                return stored.result
            resumed = stored is not None
            self._receipts.save_execution(
                request_id,
                ExecutionRecord(payload_digest=context.payload_digest, phase=ReceiptPhase.BEGUN),
            )
            facts = await self._perform(request, resumed=resumed)
            result = self._result(request, context, facts)
            if facts.status is not ObservationStatus.UNKNOWN:
                self._receipts.save_execution(
                    request_id,
                    ExecutionRecord(
                        payload_digest=context.payload_digest,
                        phase=ReceiptPhase.DONE,
                        result=result,
                    ),
                )
            return result

    @staticmethod
    def _refusal(request_id: RequestId, detail: str) -> ExecutorRefusal:
        return ExecutorRefusal(request_id=request_id, role=ExecutorRole.WORKSPACES, detail=detail)

    @staticmethod
    def _result(request: Request, context: ExecutionContext, facts: _Facts) -> ExecutionResult:
        request_id = request.request_id
        assert request_id is not None  # noqa: S101  # lint-waiver: LW-402301 [S101]; execute() rejects a missing identity before translation.
        observation = Observation(
            event_id=EventId(root=f"{request_id.root}:observation:0"),
            request_id=request_id,
            scope=request.scope,
            admission_id=request.admission_id,
            sequence=0,
            observed_at=context.now_at,
            status=facts.status,
            resource_id=facts.resource_id,
            accepted=facts.accepted,
            terminal=facts.terminal,
            released=facts.released,
            children=(),
            children_complete=facts.children_complete,
            diagnostic=facts.diagnostic,
        )
        events: tuple[OwnerEvent, ...] = ()
        match request:
            case SnapshotAndRetainRun():
                events = (
                    RunInvocationCheckpointObserved(
                        invocation=request.invocation,
                        checkpoint_request=request_id,
                        observation=observation,
                        revision=facts.revision,
                    ),
                )
            case AdoptRevision() | VerifyAdoption():
                events = (AdoptionObserved(observation=observation, revision=facts.revision),)
            case EnsureWorkspace() | RestoreRevision():
                events = (
                    WorkspaceObserved(
                        attempt=request.attempt,
                        observation=observation,
                        revision=facts.revision,
                    ),
                )
            case _:
                pass
        return ExecutionResult(
            observation=RequestObserved(
                observation=observation,
                setup_failure=facts.setup_failure,
                revision=facts.revision,
            ),
            owner_events=events,
        )

    async def _perform(self, request: Request, *, resumed: bool) -> _Facts:
        match request:
            case EnsureWorkspace():
                return await self._ensure(request, resumed=resumed)
            case SnapshotAndRetainRun():
                return await self._snapshot(
                    self._workspaces.root,
                    request,
                    request.retention,
                    resumed=resumed,
                    resource_id=None,
                )
            case AdoptRevision() | VerifyAdoption():
                return await self._adoption(request, resumed=resumed)
            case RestoreRevision() | RetainRevision() | SnapshotAndRetain() | DiscardWorkspace():
                return await self._attempt_request(request, resumed=resumed)
            case _:
                return _rejected(f"{request.kind} cannot be executed against a workspace")

    async def _attempt_request(
        self,
        request: RestoreRevision | RetainRevision | SnapshotAndRetain | DiscardWorkspace,
        *,
        resumed: bool,
    ) -> _Facts:
        binding = self._receipts.load_binding(request.attempt)
        if binding is None:
            return _rejected("no workspace was ensured for this attempt")
        workspace = self._bound_workspace(request.attempt, binding)
        if isinstance(request, DiscardWorkspace):
            return await self._discard(request, binding, workspace)
        if workspace is None:
            return _rejected(
                "the attempt's workspace is no longer live", resource_id=binding.resource_id
            )
        match request:
            case RestoreRevision():
                return await self._restore(request, binding, workspace, resumed=resumed)
            case RetainRevision():
                return await self._retain(request, binding, workspace)
            case _:
                return await self._snapshot(
                    workspace,
                    request,
                    request.retention,
                    resumed=resumed,
                    resource_id=binding.resource_id,
                )

    async def _adoption(self, request: AdoptRevision | VerifyAdoption, *, resumed: bool) -> _Facts:
        root = self._workspaces.root
        ref = request.selection.revision
        commit = await self._known_revision(root, ref)
        if commit is None:
            return _rejected("selected revision is not a canonical revision of this run")
        if isinstance(request.selection, TrustedBaseline) and commit != root.trusted_input_baseline:
            return _rejected("selected baseline is not the run's trusted input baseline")
        applied = await root.matches_revision(commit)
        if isinstance(request, VerifyAdoption):
            if not applied:
                return _unknown("root workspace does not yet prove the selected content")
        elif not (resumed and applied):
            # Inspect before replay: a prior host may have applied it already. Otherwise
            # a clean restore to the same revision is the idempotent effect.
            await self._workspaces.adopt(commit)
        return _Facts(ObservationStatus.SUCCEEDED, accepted=True, revision=ref)

    def _bound_workspace(
        self, attempt: AttemptRef, binding: AttemptBinding
    ) -> RuntimeWorkspace | None:
        if binding.workspace_id is None:
            return self._workspaces.root
        return self._workspaces.live_candidate(_member_id(attempt))

    async def _known_revision(self, workspace: RuntimeWorkspace, ref: RevisionRef) -> str | None:
        commit = _commit_of(ref)
        if commit is None or not await workspace.has_revision(commit):
            return None
        return commit

    async def _ensure(self, request: EnsureWorkspace, *, resumed: bool) -> _Facts:
        plan = request.plan
        attempt = request.attempt
        existing = self._receipts.load_binding(attempt)
        if existing is not None and (existing.mode, existing.base) != (plan.mode, plan.base):
            return _rejected(
                "attempt is already bound to another workspace plan",
                resource_id=existing.resource_id,
            )
        base = await self._known_revision(self._workspaces.root, plan.base)
        if base is None:
            return _rejected("base revision is not a revision of this run")
        if plan.mode is WorkspaceMode.EXCLUSIVE_ROOT:
            return self._ensure_root(request)
        return await self._ensure_candidate(request, existing, base, resumed=resumed)

    def _ensure_root(self, request: EnsureWorkspace) -> _Facts:
        resource_id = ResourceId(root=f"workspace-root:{_attempt_key(request.attempt)}")
        self._bind(request.attempt, request, resource_id, None, self._workspaces.root.path)
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            resource_id=resource_id,
            revision=request.plan.base,
        )

    async def _ensure_candidate(
        self,
        request: EnsureWorkspace,
        existing: AttemptBinding | None,
        base: str,
        *,
        resumed: bool,
    ) -> _Facts:
        plan = request.plan
        attempt = request.attempt
        live = self._workspaces.live_candidate(_member_id(attempt))
        if live is None:
            if resumed and existing is None:
                # The intent is durable but no candidate or binding is: a prior host may
                # have created one that this process cannot see. Never create a second.
                return _unknown("candidate creation outcome unknown after restart")
            if existing is not None:
                return _rejected("attempt workspace was released", resource_id=existing.resource_id)
            try:
                live = await self._workspaces.create_candidate(base, member_id=_member_id(attempt))
            except (RuntimeContractError, WorkspaceRestoreError) as error:
                return _Facts(
                    ObservationStatus.FAILED,
                    diagnostic=str(error),
                    setup_failure=SetupFailureKind.UNKNOWN,
                )
        assert request.request_id is not None  # noqa: S101  # lint-waiver: LW-402305 [S101]; execute() rejects a missing identity.
        resource_id = (
            existing.resource_id
            if existing is not None
            else ResourceId(root=f"workspace:{_attempt_key(attempt)}:{request.request_id.root}")
        )
        self._bind(attempt, request, resource_id, live.id, live.path)
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            resource_id=resource_id,
            revision=plan.base,
        )

    def _bind(
        self,
        attempt: AttemptRef,
        request: EnsureWorkspace,
        resource_id: ResourceId,
        workspace_id: str | None,
        path: Path,
    ) -> None:
        if self._receipts.load_binding(attempt) is not None:
            return
        assert request.request_id is not None  # noqa: S101  # lint-waiver: LW-402302 [S101]; execute() rejects a missing identity.
        self._receipts.save_binding(
            attempt,
            AttemptBinding(
                ensure_request=request.request_id,
                mode=request.plan.mode,
                base=request.plan.base,
                resource_id=resource_id,
                workspace_id=workspace_id,
                path=str(path),
            ),
        )

    async def _restore(
        self,
        request: RestoreRevision,
        binding: AttemptBinding,
        workspace: RuntimeWorkspace,
        *,
        resumed: bool,
    ) -> _Facts:
        commit = await self._known_revision(workspace, request.revision)
        if commit is None:
            return _rejected(
                "revision is not a canonical revision of this run", resource_id=binding.resource_id
            )
        if resumed:
            return _unknown(
                "restore outcome unknown after interruption", resource_id=binding.resource_id
            )
        try:
            await workspace.restore(commit)
        except WorkspaceRestoreError as error:
            return _rejected(str(error), resource_id=binding.resource_id)
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            resource_id=binding.resource_id,
            revision=request.revision,
        )

    async def _retain(
        self, request: RetainRevision, binding: AttemptBinding, workspace: RuntimeWorkspace
    ) -> _Facts:
        commit = await self._known_revision(workspace, request.revision)
        if commit is None:
            return _rejected(
                "revision is not a canonical revision of this run", resource_id=binding.resource_id
            )
        assert request.request_id is not None  # noqa: S101  # lint-waiver: LW-402303 [S101]; execute() rejects a missing identity.
        # Retention is an idempotent ref update under a per-request label.
        await workspace.retain(commit, label=f"{request.retention}:{request.request_id.root}")
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            resource_id=binding.resource_id,
            revision=request.revision,
        )

    async def _snapshot(
        self,
        workspace: RuntimeWorkspace,
        request: SnapshotAndRetain | SnapshotAndRetainRun,
        retention: Literal["wip", "candidate"],
        *,
        resumed: bool,
        resource_id: ResourceId | None,
    ) -> _Facts:
        if resumed:
            return _unknown("snapshot outcome unknown after interruption", resource_id=resource_id)
        assert request.request_id is not None  # noqa: S101  # lint-waiver: LW-402304 [S101]; execute() rejects a missing identity.
        commit = await workspace.snapshot_and_retain(
            f"core:{request.request_id.root}",
            retention_label=f"{retention}:{request.request_id.root}",
        )
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            resource_id=resource_id,
            revision=revision_ref(commit),
        )

    async def _discard(
        self,
        request: DiscardWorkspace,
        binding: AttemptBinding,
        workspace: RuntimeWorkspace | None,
    ) -> _Facts:
        del request
        if binding.workspace_id is None:
            return _rejected(
                "the exclusive root workspace is run-owned and cannot be discarded",
                resource_id=binding.resource_id,
            )
        if isinstance(workspace, RuntimeCandidateWorkspace):
            try:
                await workspace.discard()
            except BaseExceptionGroup as error:
                return _Facts(
                    ObservationStatus.FAILED,
                    terminal=False,
                    accepted=True,
                    resource_id=binding.resource_id,
                    diagnostic=f"discard failed: {error}",
                )
        if await asyncio.to_thread(Path(binding.path).exists):
            return _unknown(
                "workspace directory still exists after discard", resource_id=binding.resource_id
            )
        # Candidate sessions close inside the same lifecycle step and a closed handle
        # admits none, so the release manifest of this workspace is complete and empty.
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            released=True,
            children_complete=True,
            resource_id=binding.resource_id,
        )


__all__ = [
    "AttemptBinding",
    "DirectoryWorkspaceReceipts",
    "ExecutionRecord",
    "ReceiptPhase",
    "RuntimeWorkspaceRequests",
    "WorkspaceReceipts",
    "revision_ref",
]
