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
digest today (see the lane handoff). A reference is accepted only when the digest
matches the commit, the commit exists, and the run knows it: it is the root head,
the trusted baseline, the attempt's base, or a revision this executor recorded
as made by the same attempt or retained for the run. Foreign attempts' snapshots
and dangling objects are rejected.

The executor never returns ``ExecutorRefusal``: the shell halts on a refusal
after authorization, so every inability is a typed observation. Only terminal
observations are stored; Unknown and retryable failures are not.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

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
    OwnerEvent,
)
from vs_runtime._workspace_receipts import (
    AttemptBinding,
    ExecutionRecord,
    ReceiptCorruptError,
    ReceiptPhase,
    RootGrant,
    WorkspaceReceipts,
    attempt_key,
)
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


def _member_id(attempt: AttemptRef) -> str:
    """Stable member identity: one candidate path per attempt generation."""
    return "attempt-" + hashlib.sha256(attempt_key(attempt).encode()).hexdigest()[:24]


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


def _retryable(diagnostic: str, resource_id: ResourceId | None) -> _Facts:
    """A failure that a later identical request may overcome; never stored."""
    return _Facts(
        ObservationStatus.FAILED, terminal=False, resource_id=resource_id, diagnostic=diagnostic
    )


_RUN_OWNER = "run"
_HANDLED = (EnsureWorkspace, RestoreRevision, SnapshotAndRetain, RetainRevision, DiscardWorkspace)


class RuntimeWorkspaceRequests:
    """Translate workspace requests into ``RuntimeWorkspaces`` calls, once each."""

    def __init__(self, workspaces: RuntimeWorkspaces, receipts: WorkspaceReceipts) -> None:
        self._workspaces = workspaces
        self._receipts = receipts
        self._locks: dict[str, asyncio.Lock] = {}

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionOutcome:
        """Execute, replay or inspect one request under its canonical identity."""
        request_id = request.request_id
        if request_id is None:
            message = "request_id: execution requires a canonical identity"
            raise ValueError(message)
        if not isinstance(
            request, (*_HANDLED, SnapshotAndRetainRun, AdoptRevision, VerifyAdoption)
        ):
            return self._result(
                request, context, _rejected(f"{request.kind} is not executed by this role")
            )
        owner = f"{request.scope.owner.kind}:{request.scope.owner.root}:{request.scope.generation}"
        lock = self._locks.setdefault(self._serialization_key(request), asyncio.Lock())
        async with lock:
            if not self._receipts.check_fence(owner, context.fence):
                raise ContractError(("fence",), "stale execution host")
            try:
                return await self._execute_once(request, context)
            except ReceiptCorruptError as error:
                return self._result(request, context, _unknown(str(error)))

    @staticmethod
    def _serialization_key(request: Request) -> str:
        if isinstance(request, _HANDLED):
            return attempt_key(request.attempt)
        return "root"

    async def _execute_once(self, request: Request, context: ExecutionContext) -> ExecutionOutcome:
        request_id = request.request_id
        assert request_id is not None  # noqa: S101  # lint-waiver: LW-402307 [S101]; execute() rejects a missing identity.
        stored = self._receipts.begin_execution(
            request_id,
            ExecutionRecord(payload_digest=context.payload_digest, phase=ReceiptPhase.BEGUN),
        )
        if stored is not None and stored.payload_digest != context.payload_digest:
            return self._result(
                request, context, _rejected("same request identity with another payload")
            )
        if stored is not None and stored.result is not None:
            return stored.result
        lease = context.lease
        if lease is not None and not lease.verify(now_at=context.now_at):
            return self._result(request, context, _unknown("host authority is no longer held"))
        facts = await self._perform(request, resumed=stored is not None)
        result = self._result(request, context, facts)
        if not facts.terminal:
            return result
        if lease is not None and not lease.verify(now_at=context.now_at):
            return self._result(request, context, _unknown("host authority lost before recording"))
        self._receipts.save_execution(
            request_id,
            ExecutionRecord(
                payload_digest=context.payload_digest, phase=ReceiptPhase.DONE, result=result
            ),
        )
        return result

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
                    owner=_RUN_OWNER,
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
        if isinstance(request, DiscardWorkspace):
            return await self._discard(request, binding)
        workspace = await self._bound_workspace(request.attempt, binding)
        if isinstance(workspace, _Facts):
            return workspace
        match request:
            case RestoreRevision():
                return await self._restore(request, binding, workspace)
            case RetainRevision():
                return await self._retain(request, binding, workspace)
            case _:
                return await self._snapshot(
                    workspace,
                    request,
                    request.retention,
                    resumed=resumed,
                    resource_id=binding.resource_id,
                    owner=request.attempt.attempt_id.root,
                )

    async def _bound_workspace(
        self, attempt: AttemptRef, binding: AttemptBinding
    ) -> RuntimeWorkspace | _Facts:
        """Return the attempt's live workspace, reopening it from disk after a restart."""
        if binding.workspace_id is None:
            if self._receipts.root_holder() != attempt:
                return _rejected(
                    "the exclusive root is held by another attempt generation",
                    resource_id=binding.resource_id,
                )
            return self._workspaces.root
        member = _member_id(attempt)
        live = self._workspaces.live_candidate(member) or await self._workspaces.reattach_candidate(
            member
        )
        if live is None:
            return _rejected(
                "the attempt's workspace no longer exists", resource_id=binding.resource_id
            )
        return live

    async def _known_revision(
        self,
        ref: RevisionRef,
        *,
        owner: str | None,
        extra: str | None = None,
        retained_by_any: bool = False,
    ) -> str | None:
        """Return the commit if canonical, present and known to this run (see module doc)."""
        commit = _commit_of(ref)
        root = self._workspaces.root
        if commit is None or not await root.has_revision(commit):
            return None
        if commit in {root.revision, root.trusted_input_baseline, extra}:
            return commit
        owners = self._receipts.revision_owners(commit)
        if _RUN_OWNER in owners or (owner is not None and owner in owners):
            return commit
        return commit if retained_by_any and owners else None

    async def _adoption(self, request: AdoptRevision | VerifyAdoption, *, resumed: bool) -> _Facts:
        root = self._workspaces.root
        ref = request.selection.revision
        commit = await self._known_revision(
            ref,
            owner=None,
            retained_by_any=not isinstance(request.selection, TrustedBaseline),
        )
        if commit is None:
            return _rejected("selected revision is not a retained revision of this run")
        if isinstance(request.selection, TrustedBaseline) and commit != root.trusted_input_baseline:
            return _rejected("selected baseline is not the run's trusted input baseline")
        applied = await root.matches_revision(commit)
        if isinstance(request, VerifyAdoption):
            if not applied:
                return _unknown("root workspace does not yet prove the selected content")
        elif not (resumed and applied):
            failure = await self._materialize(root, commit)
            if failure is not None:
                return failure
        return _Facts(ObservationStatus.SUCCEEDED, accepted=True, revision=ref)

    @staticmethod
    async def _materialize(
        workspace: RuntimeWorkspace, commit: str, *, resource_id: ResourceId | None = None
    ) -> _Facts | None:
        """Make the tree exactly *commit* and prove it; ``None`` means success."""
        try:
            await workspace.restore(commit, clean_ignored=True)
        except WorkspaceRestoreError as error:
            return _retryable(f"restore failed: {error}", resource_id)
        if not await workspace.matches_revision(commit):
            return _retryable("tree does not match the revision after restore", resource_id)
        return None

    async def _ensure(self, request: EnsureWorkspace, *, resumed: bool) -> _Facts:
        plan = request.plan
        attempt = request.attempt
        existing = self._receipts.load_binding(attempt)
        if existing is not None and (existing.mode, existing.base) != (plan.mode, plan.base):
            return _rejected(
                "attempt is already bound to another workspace plan",
                resource_id=existing.resource_id,
            )
        base = await self._known_revision(
            plan.base,
            owner=attempt.attempt_id.root,
            extra=_commit_of(existing.base) if existing else None,
        )
        if base is None:
            return _rejected("base revision is not a revision of this run")
        if plan.mode is WorkspaceMode.EXCLUSIVE_ROOT:
            return self._ensure_root(request)
        return await self._ensure_candidate(request, existing, base, resumed=resumed)

    def _ensure_root(self, request: EnsureWorkspace) -> _Facts:
        attempt = request.attempt
        resource_id = ResourceId(root=f"workspace-root:{attempt_key(attempt)}")
        match self._receipts.acquire_root(attempt):
            case RootGrant.HELD:
                return _unknown("the exclusive root is held by another attempt")
            case RootGrant.SUPERSEDED:
                return _rejected("a newer generation of this attempt holds the exclusive root")
            case RootGrant.GRANTED:
                pass
        self._bind(attempt, request, resource_id, None)
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
        del resumed
        attempt = request.attempt
        member = _member_id(attempt)
        live = self._workspaces.live_candidate(member)
        if live is None:
            # A host that stopped after creating the worktree leaves it on disk:
            # reopen it with its content instead of replacing it or reporting loss.
            live = await self._workspaces.reattach_candidate(member)
        if live is None:
            if existing is not None:
                return _rejected("attempt workspace was released", resource_id=existing.resource_id)
            try:
                live = await self._workspaces.create_candidate(base, member_id=member)
            except (RuntimeContractError, WorkspaceRestoreError) as error:
                return _retryable(str(error), None)
        assert request.request_id is not None  # noqa: S101  # lint-waiver: LW-402305 [S101]; execute() rejects a missing identity.
        resource_id = (
            existing.resource_id
            if existing is not None
            else ResourceId(root=f"workspace:{attempt_key(attempt)}:{request.request_id.root}")
        )
        binding = self._bind(attempt, request, resource_id, live.id)
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            resource_id=binding.resource_id,
            revision=request.plan.base,
        )

    def _bind(
        self,
        attempt: AttemptRef,
        request: EnsureWorkspace,
        resource_id: ResourceId,
        workspace_id: str | None,
    ) -> AttemptBinding:
        assert request.request_id is not None  # noqa: S101  # lint-waiver: LW-402302 [S101]; execute() rejects a missing identity.
        return self._receipts.bind(
            attempt,
            AttemptBinding(
                ensure_request=request.request_id,
                mode=request.plan.mode,
                base=request.plan.base,
                resource_id=resource_id,
                workspace_id=workspace_id,
            ),
        )

    async def _restore(
        self, request: RestoreRevision, binding: AttemptBinding, workspace: RuntimeWorkspace
    ) -> _Facts:
        commit = await self._known_revision(
            request.revision,
            owner=request.attempt.attempt_id.root,
            extra=_commit_of(binding.base),
        )
        if commit is None:
            return _rejected(
                "revision is not a revision this attempt may restore",
                resource_id=binding.resource_id,
            )
        # Restore is idempotent, so a resumed request only repeats it when the tree differs.
        if not await workspace.matches_revision(commit):
            failure = await self._materialize(workspace, commit, resource_id=binding.resource_id)
            if failure is not None:
                return failure
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            resource_id=binding.resource_id,
            revision=request.revision,
        )

    async def _retain(
        self, request: RetainRevision, binding: AttemptBinding, workspace: RuntimeWorkspace
    ) -> _Facts:
        commit = await self._known_revision(
            request.revision,
            owner=request.attempt.attempt_id.root,
            extra=_commit_of(binding.base),
        )
        if commit is None:
            return _rejected(
                "revision is not a revision this attempt may retain",
                resource_id=binding.resource_id,
            )
        assert request.request_id is not None  # noqa: S101  # lint-waiver: LW-402303 [S101]; execute() rejects a missing identity.
        # Retention is an idempotent ref update under a per-request label.
        await workspace.retain(commit, label=f"{request.retention}:{request.request_id.root}")
        self._receipts.record_revision(commit, _RUN_OWNER)
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            resource_id=binding.resource_id,
            revision=request.revision,
        )

    async def _snapshot(  # noqa: PLR0913  # lint-waiver: LW-402312 [PLR0913]; the snapshot inputs are independent facts of one request and a wrapper type would only rename them.
        self,
        workspace: RuntimeWorkspace,
        request: SnapshotAndRetain | SnapshotAndRetainRun,
        retention: Literal["wip", "candidate"],
        *,
        resumed: bool,
        resource_id: ResourceId | None,
        owner: str,
    ) -> _Facts:
        assert request.request_id is not None  # noqa: S101  # lint-waiver: LW-402304 [S101]; execute() rejects a missing identity.
        label = f"core:{request.request_id.root}"
        retention_label = f"{retention}:{request.request_id.root}"
        # An interrupted snapshot may already have committed: recover it by its
        # deterministic label rather than commit a second time.
        commit = await workspace.find_snapshot(label) if resumed else None
        if commit is None:
            commit = await workspace.snapshot_and_retain(label, retention_label=retention_label)
        else:
            await workspace.retain(commit, label=retention_label)
        self._receipts.record_revision(commit, owner)
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            resource_id=resource_id,
            revision=revision_ref(commit),
        )

    async def _discard(self, request: DiscardWorkspace, binding: AttemptBinding) -> _Facts:
        if binding.workspace_id is None:
            return _rejected(
                "the exclusive root workspace is run-owned and cannot be discarded",
                resource_id=binding.resource_id,
            )
        member = _member_id(request.attempt)
        candidate = self._workspaces.live_candidate(member)
        if candidate is None:
            candidate = await self._workspaces.reattach_candidate(member)
        if candidate is None:
            # Someone else released it. Claim completeness only with a recorded discard.
            recorded = self._receipts.is_released(request.attempt)
            return _Facts(
                ObservationStatus.SUCCEEDED,
                accepted=True,
                released=True,
                children_complete=recorded,
                resource_id=binding.resource_id,
                diagnostic="" if recorded else "released without a recorded session close",
            )
        path = candidate.path
        try:
            await candidate.discard()
        except (OSError, BaseExceptionGroup) as error:
            return _Facts(
                ObservationStatus.FAILED,
                terminal=False,
                accepted=True,
                resource_id=binding.resource_id,
                diagnostic=f"discard failed: {error}",
            )
        if await asyncio.to_thread(path.exists):
            return _unknown(
                "workspace directory still exists after discard", resource_id=binding.resource_id
            )
        # discard() closed the candidate's sessions under the lifecycle lock before
        # removing it, and returned without error: that outcome is the evidence.
        self._receipts.mark_released(request.attempt)
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            released=True,
            children_complete=True,
            resource_id=binding.resource_id,
        )


__all__ = ["RuntimeWorkspaceRequests", "revision_ref"]
