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
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Protocol, assert_never

from vs_core.api import (
    AdoptionObserved,
    AdoptRevision,
    AttemptRef,
    DiscardWorkspace,
    EnsureWorkspace,
    ObservationStatus,
    RequestObserved,
    ResourceId,
    RestoreRevision,
    RetainRevision,
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
from vs_project.api import NestedRepositoryError
from vs_runtime._core_identity import core_identity
from vs_runtime._core_requests import (
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    OwnerEvent,
)
from vs_runtime._effect_fence import (
    EffectFences,
    Entered,
    FenceKey,
    FenceKind,
    MayHold,
    NothingHeld,
)
from vs_runtime._observation_factory import (
    ObservationFactory,
    ObservationFacts,
    ObservationSubject,
)
from vs_runtime._receipt_store import (
    Conflict,
    Declined,
    Performed,
    Replayed,
    Settled,
    Transient,
    owner_key,
)
from vs_runtime._workspace_lookup import candidate_member_id, find_attempt_workspace
from vs_runtime._workspace_receipts import (
    AttemptBinding,
    RootBaseline,
    RootGrant,
    RootRelease,
    StoreWorkspaceReceipts,
    WorkspaceReceipts,
    attempt_key,
)
from vs_runtime.contracts import RuntimeContractError, WorkspaceRestoreError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_core.api import Request
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._workspaces import RuntimeWorkspace, RuntimeWorkspaces
    from vs_sim.api import BlockingRunner

_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def revision_ref(commit: str) -> RevisionRef:
    """Return the canonical core reference of one Git commit."""
    return RevisionRef.of_git_commit(commit)


def commit_of(ref: RevisionRef) -> str | None:
    """Return the commit named by a reference, or ``None`` if it is not canonical.

    Core's ``RevisionRef.git_commit`` is the one reader of the digest scheme; this
    only adds the workspace's own rule that a commit is a full 40-digit object name.
    """
    commit = ref.git_commit
    return commit if commit is not None and _COMMIT.match(commit) is not None else None


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
    holding_nothing: ObservationFacts | None = None
    """The facts as ``refused`` or ``never_held`` built them, when the answer holds nothing."""


def _conflict() -> _Facts:
    """The refusal of a reused identity: it proves nothing about what the identity holds."""
    facts = ObservationFacts.payload_conflict()
    return _Facts(
        facts.status,
        terminal=facts.terminal,
        released=facts.released,
        children_complete=facts.children_complete,
        diagnostic=facts.diagnostic,
        setup_failure=SetupFailureKind.PERMANENT,
    )


def _rejected(diagnostic: str) -> _Facts:
    """A refusal decided before the request did anything: it holds nothing, now or later.

    The refusal is sealed, so a replay answers the same and the request never acts afterwards.
    Like ``ObservationFacts.never_held`` it names no resource, so core never reads it as the
    release of a workspace that the attempt (or another one) still holds.
    """
    return _holding_nothing(ObservationFacts.refused(diagnostic))


def _never_held(proof: NothingHeld, attempt: AttemptRef, diagnostic: str) -> _Facts:
    """The discard's answer that the attempt holds no workspace, now or later (a closed fence)."""
    return _holding_nothing(
        ObservationFacts.never_held(proof, workspace_fence(attempt), diagnostic)
    )


def _holding_nothing(facts: ObservationFacts) -> _Facts:
    return _Facts(
        facts.status,
        released=facts.released,
        children_complete=facts.children_complete,
        diagnostic=facts.diagnostic,
        setup_failure=SetupFailureKind.PERMANENT,
        holding_nothing=facts,
    )


def workspace_fence(attempt: AttemptRef) -> FenceKey:
    """The fence of one attempt generation's workspace: ensure enters it, discard closes it."""
    return FenceKey(FenceKind.ATTEMPT_WORKSPACE, attempt_key(attempt))


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
_WITHDRAWN = "the attempt's workspace was withdrawn before any ensure bound it"
_HANDLED = (EnsureWorkspace, RestoreRevision, SnapshotAndRetain, RetainRevision, DiscardWorkspace)


class RunInvocationProof(Protocol):
    """Proof that a run-owned invocation's writer can no longer change the root workspace."""

    def unproven(self, request: SnapshotAndRetainRun) -> str | None:
        """None when the request's invocation is proven terminal, else why it is not."""
        ...


class RuntimeWorkspaceRequests:
    """Translate workspace requests into ``RuntimeWorkspaces`` calls, once each."""

    def __init__(
        self,
        workspaces: RuntimeWorkspaces,
        store: ReceiptStore,
        run_invocations: RunInvocationProof,
        blocking: BlockingRunner,
    ) -> None:
        """Bind the workspaces to the shared store and to the proof run snapshots require.

        A run snapshot retains the root only after *run_invocations* proves the
        invocation's writer ended; a missing proof is Unknown and snapshots nothing.
        *blocking* runs the executor's own disk checks off the event loop.
        """
        self._blocking = blocking
        self._workspaces = workspaces
        self._run_invocations = run_invocations
        self._store = store
        self._receipts: WorkspaceReceipts = StoreWorkspaceReceipts(store)
        self._observations = ObservationFactory(store)
        self._fences = EffectFences(store)
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
        lock = self._locks.setdefault(self._serialization_key(request), asyncio.Lock())
        async with lock:

            async def perform(
                *, resumed: bool
            ) -> Settled[ExecutionResult] | Transient[ExecutionResult]:
                result = self._result(
                    request, context, await self._perform(request, context, resumed=resumed)
                )
                observation = result.observation.observation
                return Settled(result) if observation.terminal else Transient(result)

            execution = await self._store.run_once(
                request_id.root,
                owner=owner_key(request),
                context=context,
                result_type=ExecutionResult,
                perform=perform,
            )
        match execution:
            case Replayed(result) | Performed(result):
                return result
            case Conflict():
                return self._result(request, context, _conflict())
            case Declined(reason):
                return self._result(request, context, _unknown(reason))
            case _:
                assert_never(execution)

    @staticmethod
    def _serialization_key(request: Request) -> str:
        if isinstance(request, _HANDLED):
            return attempt_key(request.attempt)
        return "root"

    def _result(
        self, request: Request, context: ExecutionContext, facts: _Facts
    ) -> ExecutionResult:
        request_id = request.request_id
        assert request_id is not None  # noqa: S101  # lint-waiver: LW-402301 [S101]; execute() rejects a missing identity before translation.
        observed = facts.holding_nothing or ObservationFacts(
            status=facts.status,
            terminal=facts.terminal,
            accepted=facts.accepted,
            released=facts.released,
            children_complete=facts.children_complete,
            resource_id=facts.resource_id,
            diagnostic=facts.diagnostic,
            revision=facts.revision,
        )
        observation = self._observations.observe(
            ObservationSubject.of(request), observed, observed_at=context.now_at
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

    async def _perform(
        self, request: Request, context: ExecutionContext, *, resumed: bool
    ) -> _Facts:
        if isinstance(request, _HANDLED) and not self._receipts.admit_generation(request.attempt):
            return _rejected("a newer generation of this attempt exists")
        match request:
            case EnsureWorkspace():
                return await self._fenced_ensure(request, context)
            case SnapshotAndRetainRun():
                return await self._run_snapshot(request, resumed=resumed)
            case AdoptRevision() | VerifyAdoption():
                return await self._adoption(request, resumed=resumed)
            case RestoreRevision() | RetainRevision() | SnapshotAndRetain() | DiscardWorkspace():
                return await self._attempt_request(request, context, resumed=resumed)
            case _:
                return _rejected(f"{request.kind} cannot be executed against a workspace")

    async def _run_snapshot(self, request: SnapshotAndRetainRun, *, resumed: bool) -> _Facts:
        """Retain the run's root, but only once the invocation's writer is proven gone."""
        unproven = self._run_invocations.unproven(request)
        if unproven is not None:
            return _unknown(unproven)
        return await self._snapshot(
            self._workspaces.root,
            request,
            request.retention,
            resumed=resumed,
            resource_id=None,
            owner=_RUN_OWNER,
        )

    async def _withdraw_or_discard(
        self, request: DiscardWorkspace, context: ExecutionContext, *, resumed: bool
    ) -> _Facts:
        """Close the attempt's workspace fence, then discard what its ensures made, if anything."""
        attempt = request.attempt
        with self._store.exclusive():
            closure = self._fences.close(
                workspace_fence(attempt), by=core_identity(request), host=context.fence
            )
            bound = self._receipts.load_binding(attempt)
            if bound is None:
                # The generation ends unbound: it never holds the exclusive root again.
                self._receipts.mark_released(attempt)
        if bound is not None:
            return await self._discard(request, bound, resumed=resumed)
        if isinstance(closure, NothingHeld):
            return _never_held(closure, attempt, _WITHDRAWN)
        return await self._remove_unbound(attempt, closure)

    async def _remove_unbound(self, attempt: AttemptRef, closure: MayHold) -> _Facts:
        """Remove a worktree an ensure created but never bound, once no ensure can still create.

        An ensure that stopped between creating its worktree and binding it left the worktree
        behind: removing it makes "nothing held" true now, and the closed fence keeps it so.
        """
        if closure.live:
            return _unknown("an ensure of the attempt's workspace is in flight")
        member = candidate_member_id(attempt)

        async def find() -> Callable[[], Awaitable[None]] | None:
            leftover = self._workspaces.live_candidate(member)
            if leftover is None:
                leftover = await self._workspaces.reattach_candidate(member)
            return None if leftover is None else leftover.discard

        try:
            removed = await self._fences.remove(closure, find)
        except (OSError, BaseExceptionGroup) as error:
            return _unknown(f"an unbound worktree of the attempt was not removed: {error}")
        return _never_held(self._fences.cleared(closure, removed), attempt, _WITHDRAWN)

    async def _attempt_request(
        self,
        request: RestoreRevision | RetainRevision | SnapshotAndRetain | DiscardWorkspace,
        context: ExecutionContext,
        *,
        resumed: bool,
    ) -> _Facts:
        if isinstance(request, DiscardWorkspace):
            return await self._withdraw_or_discard(request, context, resumed=resumed)
        binding = self._receipts.load_binding(request.attempt)
        if binding is None:
            return _rejected("no workspace was ensured for this attempt")
        workspace = await self._bound_workspace(request.attempt, binding)
        if isinstance(workspace, _Facts):
            return workspace
        match request:
            case RestoreRevision():
                return await self._restore(request, binding, workspace)
            case RetainRevision():
                return await self._retain(request, binding, workspace)
            case _:
                facts = await self._snapshot(
                    workspace,
                    request,
                    request.retention,
                    resumed=resumed,
                    resource_id=binding.resource_id,
                    owner=attempt_key(request.attempt),
                )
                # An invocation's checkpoint is one of many; the closure's retention is the
                # last request that keeps a revision before the discard that frees the
                # workspace, and core waits for that retention to report itself released.
                return facts if request.invocation is not None else replace(facts, released=True)

    async def _bound_workspace(
        self, attempt: AttemptRef, binding: AttemptBinding
    ) -> RuntimeWorkspace | _Facts:
        """Return the attempt's live workspace, reopening it from disk after a restart."""
        found = await find_attempt_workspace(self._workspaces, self._receipts, attempt, binding)
        if isinstance(found, str):
            return _rejected(found)
        return found

    async def _known_revision(
        self,
        ref: RevisionRef,
        *,
        owner: str | None,
        extra: str | None = None,
        retained_by_any: bool = False,
    ) -> str | None:
        """Return the commit if canonical, present and known to this run (see module doc)."""
        commit = commit_of(ref)
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
                # The root was read and its tracked content differs from the selection:
                # a conclusive mismatch, which core treats as terminal FAILED. UNKNOWN
                # stays for what could not be compared at all (a missing revision above).
                return _Facts(
                    ObservationStatus.FAILED,
                    accepted=True,
                    diagnostic="root workspace content differs from the selected revision",
                )
        elif not (resumed and applied):
            failure = await self._materialize(root, commit)
            if failure is not None:
                return failure
        return _Facts(ObservationStatus.SUCCEEDED, accepted=True, revision=ref)

    @staticmethod
    async def _materialize(
        workspace: RuntimeWorkspace,
        commit: str,
        *,
        resource_id: ResourceId | None = None,
        exact_keeping: tuple[str, ...] | None = None,
    ) -> _Facts | None:
        """Make the tree exactly *commit* and prove it; ``None`` means success."""
        try:
            await workspace.restore(commit, exact_keeping=exact_keeping)
        except WorkspaceRestoreError as error:
            return _retryable(f"restore failed: {error}", resource_id)
        if not await workspace.matches_revision(commit, exact_keeping=exact_keeping):
            return _retryable("tree does not match the revision after restore", resource_id)
        return None

    async def _fenced_ensure(self, request: EnsureWorkspace, context: ExecutionContext) -> _Facts:
        """Ensure inside the attempt's workspace fence; a discard that closed it stops the ensure."""
        fence = workspace_fence(request.attempt)
        by = core_identity(request)
        if not isinstance(self._fences.enter(fence, by=by, host=context.fence), Entered):
            # A discard closed the fence first: this ensure creates nothing.
            return _rejected(_WITHDRAWN)
        try:
            return await self._ensure(request, context)
        finally:
            # Whatever this delivery made is on disk or bound now: a discard may judge it.
            self._fences.leave(fence, by=by)

    async def _ensure(self, request: EnsureWorkspace, context: ExecutionContext) -> _Facts:
        plan = request.plan
        attempt = request.attempt
        existing = self._receipts.load_binding(attempt)
        if existing is not None and (existing.mode, existing.base) != (plan.mode, plan.base):
            return _rejected("attempt is already bound to another workspace plan")
        base = await self._known_revision(
            plan.base,
            owner=attempt_key(attempt),
            extra=commit_of(existing.base) if existing else None,
        )
        if base is None:
            return _rejected("base revision is not a revision of this run")
        if plan.mode is WorkspaceMode.EXCLUSIVE_ROOT:
            return await self._ensure_root(request, context, base=base)
        return await self._ensure_candidate(request, context, existing, base)

    async def _ensure_root(
        self, request: EnsureWorkspace, context: ExecutionContext, *, base: str
    ) -> _Facts:
        """Grant the root, and make its tree the plan's base before the attempt may use it.

        The restore runs before the binding is stored, so a crash anywhere in between
        leaves no binding and the repeated ensure grants the same generation again and
        restores again. A bound attempt is re-ensured without a restore: its edits are
        the work it is doing.
        """
        attempt = request.attempt
        resource_id = ResourceId(root=f"workspace-root:{attempt_key(attempt)}")
        match self._receipts.acquire_root(attempt):
            case RootGrant.HELD:
                return _unknown("the exclusive root is held by another attempt")
            case RootGrant.SUPERSEDED:
                return _rejected("a newer generation of this attempt holds the exclusive root")
            case RootGrant.RELEASED:
                return _rejected("this attempt generation already released the exclusive root")
            case RootGrant.GRANTED:
                pass
        if self._receipts.load_binding(attempt) is None:
            root = self._workspaces.root
            # An exclusive-root attempt owns whatever it leaves in the root, ignored files
            # included, so the grant is exactly base plus the preserved artifact dirs plus
            # what the root held before the run, which is never touched. The baseline is
            # recorded before the first restore and replayed afterwards, never re-scanned.
            baseline = await self._root_baseline(root)
            keeping = baseline.paths
            if not await root.matches_revision(base, exact_keeping=keeping):
                failure = await self._materialize(
                    root, base, resource_id=resource_id, exact_keeping=keeping
                )
                if failure is not None:
                    return failure
            if self._bind(request, context, resource_id, None) is None:
                return _rejected(_WITHDRAWN)
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            children_complete=True,
            resource_id=resource_id,
            revision=request.plan.base,
        )

    async def _root_baseline(self, root: RuntimeWorkspace) -> RootBaseline:
        """The paths the root held before the run, recorded on the first root grant."""
        stored = self._receipts.root_baseline()
        if stored is None:
            paths = await root.unversioned_paths()
            stored = self._receipts.record_root_baseline(
                RootBaseline(entries=await root.baseline_entries(paths))
            )
        return stored

    async def _check_root_baseline_drift(self) -> None:
        """Warn (through the root resource's event sink) of a changed or missing baseline path."""
        baseline = self._receipts.root_baseline()
        if baseline is not None:
            await self._workspaces.root.baseline_drift(baseline.entries)

    async def _ensure_candidate(
        self,
        request: EnsureWorkspace,
        context: ExecutionContext,
        existing: AttemptBinding | None,
        base: str,
    ) -> _Facts:
        attempt = request.attempt
        member = candidate_member_id(attempt)
        live = self._workspaces.live_candidate(member)
        if live is None:
            # A host that stopped after creating the worktree leaves it on disk:
            # reopen it with its content instead of replacing it or reporting loss.
            live = await self._workspaces.reattach_candidate(member)
        if live is None:
            if existing is not None:
                return _rejected("attempt workspace was released")
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
        binding = self._bind(request, context, resource_id, live.id)
        if binding is None:
            if existing is not None:
                # Bound before the discard closed the fence: the discard removes it.
                return _rejected("attempt workspace was released")
            # Withdrawn after this ensure created or reopened the worktree: remove it, so the
            # discard that withdrew the generation stays the truth.
            try:
                await live.discard()
            except (OSError, BaseExceptionGroup) as error:
                return _unknown(f"withdrawn workspace not removed: {error}")
            return _rejected(_WITHDRAWN)
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            children_complete=True,
            resource_id=binding.resource_id,
            revision=request.plan.base,
        )

    def _bind(
        self,
        request: EnsureWorkspace,
        context: ExecutionContext,
        resource_id: ResourceId,
        workspace_id: str | None,
    ) -> AttemptBinding | None:
        """Bind the attempt's workspace, or ``None`` when a discard closed its fence first.

        The ensure enters the fence again in the same locked step as it binds, so a discard
        that closed the fence while the workspace was being made stops the binding.
        """
        assert request.request_id is not None  # noqa: S101  # lint-waiver: LW-402302 [S101]; execute() rejects a missing identity.
        binding = AttemptBinding(
            ensure_request=request.request_id,
            mode=request.plan.mode,
            base=request.plan.base,
            resource_id=resource_id,
            workspace_id=workspace_id,
        )
        fence = workspace_fence(request.attempt)
        with self._store.exclusive():
            admission = self._fences.enter(fence, by=core_identity(request), host=context.fence)
            if not isinstance(admission, Entered):
                return None
            return self._receipts.bind(request.attempt, binding)

    async def _restore(
        self, request: RestoreRevision, binding: AttemptBinding, workspace: RuntimeWorkspace
    ) -> _Facts:
        commit = await self._known_revision(
            request.revision,
            owner=attempt_key(request.attempt),
            extra=commit_of(binding.base),
        )
        if commit is None:
            return _rejected("revision is not a revision this attempt may restore")
        # Restore is idempotent, so a resumed request only repeats it when the tree differs.
        if not await workspace.matches_revision(commit):
            failure = await self._materialize(workspace, commit, resource_id=binding.resource_id)
            if failure is not None:
                return failure
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            children_complete=True,
            resource_id=binding.resource_id,
            revision=request.revision,
        )

    async def _retain(
        self, request: RetainRevision, binding: AttemptBinding, workspace: RuntimeWorkspace
    ) -> _Facts:
        commit = await self._known_revision(
            request.revision,
            owner=attempt_key(request.attempt),
            extra=commit_of(binding.base),
        )
        if commit is None:
            return _rejected("revision is not a revision this attempt may retain")
        assert request.request_id is not None  # noqa: S101  # lint-waiver: LW-402303 [S101]; execute() rejects a missing identity.
        # Retention is an idempotent ref update under a per-request label.
        await workspace.retain(commit, label=f"{request.retention}:{request.request_id.root}")
        self._receipts.record_revision(commit, _RUN_OWNER)
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            # Keeping a revision leaves this request holding nothing. Core's closure waits for
            # the retention to report that before it issues the discard that frees the workspace.
            released=True,
            children_complete=True,
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
            try:
                commit = await workspace.snapshot_and_retain(label, retention_label=retention_label)
            except NestedRepositoryError as error:
                # The agent left a repository in its workspace that host Git must not read;
                # the snapshot fails for good, and the diagnostic says which paths.
                return _Facts(
                    ObservationStatus.FAILED,
                    accepted=True,
                    resource_id=resource_id,
                    diagnostic=str(error),
                )
        else:
            await workspace.retain(commit, label=retention_label)
        self._receipts.record_revision(commit, owner)
        return _Facts(
            ObservationStatus.SUCCEEDED,
            accepted=True,
            children_complete=True,
            resource_id=resource_id,
            revision=revision_ref(commit),
        )

    async def _discard(
        self, request: DiscardWorkspace, binding: AttemptBinding, *, resumed: bool
    ) -> _Facts:
        if binding.workspace_id is None:
            # The root is run-owned and stays; ending the attempt ends only its hold. A
            # stale generation is not the holder, so its discard changes nothing. Whether or
            # not this generation is the one releasing, this is the moment an attempt's view
            # of the root ends, so it is also when a baseline change becomes visible: check
            # before releasing, so a crash between release and the check still re-checks on
            # the inevitable replay (release is effect-once and would otherwise skip it).
            await self._check_root_baseline_drift()
            match self._receipts.release_root(request.attempt):
                case RootRelease.NOT_HOLDER:
                    return _rejected(
                        "this attempt generation never ended its hold and a newer generation"
                        " holds the exclusive root; nothing was released"
                    )
                case RootRelease.ALREADY_RELEASED:
                    diagnostic = "the exclusive root hold had already ended; nothing released now"
                case RootRelease.RELEASED:
                    diagnostic = ""
            return _Facts(
                ObservationStatus.SUCCEEDED,
                accepted=True,
                released=True,
                children_complete=True,
                resource_id=binding.resource_id,
                diagnostic=diagnostic,
            )
        member = candidate_member_id(request.attempt)
        candidate = self._workspaces.live_candidate(member)
        if candidate is None:
            candidate = await self._workspaces.reattach_candidate(member)
        if candidate is None:
            # Claim completeness only with proof the session close ran: a recorded
            # discard, or this very request begun by an earlier host (a crash between
            # the discard and its record). Anyone else's release proves nothing.
            recorded = self._receipts.is_released(request.attempt)
            if resumed and not recorded:
                self._receipts.mark_released(request.attempt)
                recorded = True
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
        if await self._blocking.run(path.exists):
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


__all__ = ["RunInvocationProof", "RuntimeWorkspaceRequests", "commit_of", "revision_ref"]
