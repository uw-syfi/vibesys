"""Private run-owned workspace collection and resource lifetime."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Protocol

from vs_runtime._workspace_access import WorkspaceAccessRecovery
from vs_runtime.contracts import (
    AgentWorkspaceRouteError,
    CaptureStopUnconfirmedError,
    RuntimeContractError,
    WorkspaceRestoreError,
    Workspaces,
    member_workspace_id,
)
from vs_sim.api import AsyncCloseOnce, NothingLeft, Released, ReleaseStack

_ROOT_CAPTURE_WORKSPACE = "root"

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from vs_core.api import AdoptionPublication, CheckoutUpdate
    from vs_runtime._agent_execution import AgentExecutionScope
    from vs_runtime._trusted_evaluation import (
        TrustedAccuracyResult,
        TrustedBenchmarkResult,
        TrustedBuildResult,
    )
    from vs_runtime._workspace_holds import WorkspaceHolds
    from vs_runtime._workspace_receipts import BaselineEntry, RootBaselineDrift
    from vs_runtime._workspace_runtime import CommandExecutionResult, WorkspaceEvaluationSpec
    from vs_runtime.contracts import CandidateWorkspace, Workspace
    from vs_sandbox.api import CaptureExecutionIdentity, CommandRunner
    from vs_sim.api import BlockingRunner, Event, OutputSink


class WorkspaceResource(Protocol):
    """Composition-owned effects for one runtime-owned workspace handle."""

    @property
    def id(self) -> str | None: ...

    @property
    def path(self) -> Path: ...

    @property
    def revision(self) -> str | None: ...

    @property
    def trusted_input_baseline(self) -> str | None: ...

    def snapshot(self, label: str) -> str: ...

    def publish(self, revision: str, message: str) -> AdoptionPublication: ...

    def update_checkout(self, publication: AdoptionPublication) -> CheckoutUpdate: ...

    def restore(
        self,
        revision: str,
        *,
        clean: bool,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
        exact_keeping: tuple[str, ...] | None = None,
    ) -> bool: ...

    def try_restore(self, revision: str, *, clean: bool) -> bool: ...

    def retain(self, revision: str, reference: str) -> None: ...

    def has_revision(self, revision: str) -> bool: ...

    def matches_revision(
        self, revision: str, *, exact_keeping: tuple[str, ...] | None = None
    ) -> bool: ...

    def unversioned_paths(self) -> tuple[str, ...]: ...

    def baseline_entries(self, paths: tuple[str, ...]) -> tuple[BaselineEntry, ...]: ...

    def baseline_drift(self, entries: tuple[BaselineEntry, ...]) -> RootBaselineDrift: ...

    def find_snapshot(self, label: str) -> str | None: ...

    def pending_changes(self) -> list[str]: ...

    def candidate_patch(self, revision: str) -> str: ...

    def measured_input(self, revision: str) -> str:
        """The digest of what the task's evaluation of *revision* reads (``GitTracker``)."""
        ...

    def withhold_unread(self) -> None:
        """Remove the paths the task declares unread, so the files are what the digest covers."""
        ...

    def is_retained(self, revision: str) -> bool: ...

    def trusted_input_changes(self) -> list[str]: ...

    def is_directory(self, path: str) -> bool: ...

    def execute(self, command: str, timeout_seconds: int | None) -> CommandExecutionResult: ...

    def execute_capture(
        self, command: str, timeout_seconds: int | None, cancel: Event
    ) -> CommandExecutionResult: ...

    @property
    def capture_execution(self) -> CaptureExecutionIdentity | None: ...

    def agent_scope(self) -> AgentExecutionScope: ...

    def agent_sandbox(self) -> CommandRunner | None:
        """Return the sandbox an agent in this workspace runs in, or ``None`` on the host."""
        ...

    @property
    def evaluation_spec(self) -> WorkspaceEvaluationSpec: ...

    async def candidate_build(self, output: OutputSink) -> TrustedBuildResult:
        """Run the candidate's build here under agent confinement, writing to *output*."""
        ...

    async def trusted_accuracy(
        self, command_override: str | None, output: OutputSink
    ) -> TrustedAccuracyResult:
        """Run trusted accuracy here, writing its command's output to *output*."""
        ...

    async def trusted_benchmark(
        self,
        command_override: str | None,
        required_metrics: frozenset[str],
        output: OutputSink,
    ) -> TrustedBenchmarkResult:
        """Run the trusted benchmark here, writing its command's output to *output*."""
        ...

    def close(self) -> None:
        """Discard: release what this workspace owns, a candidate's content included."""
        ...

    def detach(self) -> None:
        """Release this host's live handles and keep the content for ``reattach_candidate``."""
        ...


class WorkspaceResourceProvider(Protocol):
    """One coherent source of root and candidate workspace resources."""

    @property
    def root(self) -> WorkspaceResource: ...

    @property
    def supports_parallel_candidates(self) -> bool: ...

    def create_candidate(
        self, workspace_id: str, revision: str, step_tag: str | None = None, /
    ) -> WorkspaceResource: ...

    def reattach_candidate(self, workspace_id: str, revision: str, /) -> WorkspaceResource | None:
        """Reopen a candidate worktree a stopped host left on disk, keeping its content."""
        ...

    def leftover_candidates(self) -> tuple[str, ...]:
        """The workspace IDs of the candidate worktrees on disk, live or left by a stopped host."""
        ...

    def unsettled_candidates(self) -> frozenset[str]:
        """Candidate IDs durably retained because a command may still use them."""
        ...

    def record_capture_intent(
        self, workspace_id: str, execution: CaptureExecutionIdentity, /
    ) -> None:
        """Durably protect a candidate before its external command is submitted."""
        ...

    def clear_capture_intent(self, workspace_id: str, /) -> None:
        """Clear protection after the external command is confirmed settled."""
        ...

    def reconcile_capture_intents(self) -> frozenset[str]:
        """Try to settle durable captures and return those still unresolved."""
        ...

    def remove_leftover(self, workspace_id: str, /) -> None:
        """Remove a candidate worktree no live handle owns, with its Git registration."""
        ...


class OwnedWorkspaces(Workspaces, Protocol):
    """Composition lifetime for a runtime-owned workspace collection."""

    def begin_close(self) -> None:
        """Reject new work before teardown begins."""

    async def close(self) -> None:
        """Release workspaces in dependency order."""
        ...

    def follow(self, holds: Callable[[], WorkspaceHolds]) -> None:
        """Decide what outlives this host from what core references, as *holds* reads it."""
        ...

    async def release_unheld(self) -> None:
        """Discard every candidate worktree on disk that core no longer references."""
        ...


async def _drain(task: asyncio.Task[object]) -> None:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:  # noqa: BLE001  # lint-waiver: LW-228401 [BLE001]; owned resource cleanup must observe every worker outcome after cancellation.
            break


async def run_sync[**P, Result](
    runner: BlockingRunner,
    operation: Callable[P, Result],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> Result:
    task = asyncio.create_task(runner.run(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        await _drain(task)
        if error := task.exception():
            cancelled.add_note(f"workspace operation also failed: {error}")
        raise


class RuntimeWorkspace:
    """One live workspace whose effects and lifetime belong to its run."""

    def __init__(self, owner: RuntimeWorkspaces, resource: WorkspaceResource) -> None:
        self._owner = owner
        self._resource = resource
        self._id = resource.id
        self.access_recovery = WorkspaceAccessRecovery()
        if owner.access_fence is not None:
            self.guard_access(owner.access_fence)
        self._closed = False

    def guard_access(self, fenced_by: Callable[[Path], tuple[str, ...]]) -> None:
        """Refuse snapshots of this workspace while *fenced_by* names its unjudged invocations."""
        resource = self._resource
        self.access_recovery.guard(lambda: fenced_by(resource.path))

    def _ensure_open(self) -> None:
        if self._closed:
            message = "workspace is closed"
            raise ValueError(message)

    @property
    def id(self) -> str | None:
        return self._id

    @property
    def path(self) -> Path:
        self._ensure_open()
        return self._resource.path

    @property
    def revision(self) -> str | None:
        self._ensure_open()
        return self._resource.revision

    @property
    def trusted_input_baseline(self) -> str | None:
        self._ensure_open()
        return self._resource.trusted_input_baseline

    async def snapshot(self, label: str) -> str:
        await self.access_recovery.reconcile(self)
        async with self._owner._mutation(self):  # noqa: SLF001  # lint-waiver: LW-228402 [SLF001]; a workspace handle delegates synchronization to its owning collection.
            return await run_sync(self._owner.runner, self._resource.snapshot, label)

    async def restore(
        self, revision: str, *, clean: bool = True, exact_keeping: tuple[str, ...] | None = None
    ) -> None:
        """Materialize *revision*.

        *exact_keeping* also removes ignored files from any checkout, except the listed
        pre-existing paths, which stay untouched.
        """
        await self._restore(revision, clean=clean, exact_keeping=exact_keeping)

    async def _restore(
        self,
        revision: str,
        *,
        clean: bool,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
        exact_keeping: tuple[str, ...] | None = None,
    ) -> None:
        async with self._owner._mutation(self):  # noqa: SLF001  # lint-waiver: LW-228403 [SLF001]; a workspace handle delegates synchronization to its owning collection.
            restored = await run_sync(
                self._owner.runner,
                self._resource.restore,
                revision,
                clean=clean,
                preserve_paths=preserve_paths,
                preserve_memory=preserve_memory,
                exact_keeping=exact_keeping,
            )
        if not restored:
            raise WorkspaceRestoreError(revision)

    async def try_restore(self, revision: str, *, clean: bool = True) -> bool:
        async with self._owner._mutation(self):  # noqa: SLF001  # lint-waiver: LW-228411 [SLF001]; a workspace handle delegates synchronization to its owning collection.
            return await run_sync(
                self._owner.runner, self._resource.try_restore, revision, clean=clean
            )

    async def retain(self, revision: str, *, label: str) -> None:
        if not label:
            message = "workspace retention label must be nonempty"
            raise ValueError(message)
        digest = hashlib.sha256(f"{label}\0{revision}".encode()).hexdigest()
        async with self._owner._root_lock:  # noqa: SLF001  # lint-waiver: LW-228404 [SLF001]; retention mutates the collection's shared root Git metadata.
            self._ensure_open()
            await run_sync(
                self._owner.runner, self._resource.retain, revision, f"retained-{digest}"
            )

    async def snapshot_and_retain(self, label: str, *, retention_label: str) -> str:
        revision = await self.snapshot(label)
        await self.retain(revision, label=retention_label)
        return revision

    async def has_revision(self, revision: str) -> bool:
        self._ensure_open()
        return await run_sync(self._owner.runner, self._resource.has_revision, revision)

    async def matches_revision(
        self, revision: str, *, exact_keeping: tuple[str, ...] | None = None
    ) -> bool:
        self._ensure_open()
        async with self._owner._mutation(self):  # noqa: SLF001  # lint-waiver: LW-402310 [SLF001]; a workspace handle delegates synchronization to its owning collection.
            return await run_sync(
                self._owner.runner,
                self._resource.matches_revision,
                revision,
                exact_keeping=exact_keeping,
            )

    async def unversioned_paths(self) -> tuple[str, ...]:
        """The untracked and ignored paths in the tree now (directories collapsed)."""
        self._ensure_open()
        async with self._owner._mutation(self):  # noqa: SLF001  # lint-waiver: LW-402331 [SLF001]; a workspace handle delegates synchronization to its owning collection.
            return await run_sync(self._owner.runner, self._resource.unversioned_paths)

    async def baseline_entries(self, paths: tuple[str, ...]) -> tuple[BaselineEntry, ...]:
        """Fingerprint *paths* (from :meth:`unversioned_paths`) as they are now."""
        self._ensure_open()
        async with self._owner._mutation(self):  # noqa: SLF001  # lint-waiver: LW-402332 [SLF001]; a workspace handle delegates synchronization to its owning collection.
            return await run_sync(self._owner.runner, self._resource.baseline_entries, paths)

    async def baseline_drift(self, entries: tuple[BaselineEntry, ...]) -> RootBaselineDrift:
        """Compare the tree now to a recorded pre-run baseline; reports any drift found."""
        self._ensure_open()
        async with self._owner._mutation(self):  # noqa: SLF001  # lint-waiver: LW-402333 [SLF001]; a workspace handle delegates synchronization to its owning collection.
            return await run_sync(self._owner.runner, self._resource.baseline_drift, entries)

    async def find_snapshot(self, label: str) -> str | None:
        self._ensure_open()
        async with self._owner._mutation(self):  # noqa: SLF001  # lint-waiver: LW-402311 [SLF001]; a workspace handle delegates synchronization to its owning collection.
            return await run_sync(self._owner.runner, self._resource.find_snapshot, label)

    async def pending_changes(self) -> list[str]:
        self._ensure_open()
        return await run_sync(self._owner.runner, self._resource.pending_changes)

    async def restore_for_agent(
        self,
        revision: str,
        *,
        preserve_paths: tuple[str, ...],
    ) -> None:
        await self._restore(
            revision,
            clean=True,
            preserve_paths=preserve_paths,
            preserve_memory=False,
        )

    def is_directory(self, path: str) -> bool:
        return self._resource.is_directory(path)

    async def candidate_patch(self, revision: str) -> str:
        return await run_sync(self._owner.runner, self._resource.candidate_patch, revision)

    async def measured_input(self, revision: str) -> str:
        """The digest of what the task's evaluation of *revision* reads."""
        return await run_sync(self._owner.runner, self._resource.measured_input, revision)

    async def trusted_input_changes(self) -> list[str]:
        return await run_sync(self._owner.runner, self._resource.trusted_input_changes)


class RuntimeCandidateWorkspace(RuntimeWorkspace):
    """One isolated candidate with idempotent early release."""

    def __init__(self, owner: RuntimeWorkspaces, resource: WorkspaceResource) -> None:
        super().__init__(owner, resource)
        self._release = AsyncCloseOnce()

    async def discard(self) -> None:
        """Close this candidate; a failed close is tried again by the next call."""
        await self._release.run(lambda: self._owner._discard(self))  # noqa: SLF001  # lint-waiver: LW-228405 [SLF001]; candidate release is owned by its creating collection.


class RuntimeWorkspaces:
    """Own workspace handles and their teardown order."""

    def __init__(
        self,
        resources: WorkspaceResourceProvider,
        *,
        runner: BlockingRunner,
    ) -> None:
        """Own ``resources``; their synchronous effects run on ``runner``."""
        self._resources = resources
        self._runner = runner
        self._root_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._candidate_locks: dict[str, asyncio.Lock] = {}
        self._candidates: dict[str, RuntimeCandidateWorkspace] = {}
        self._members: set[str] = set()
        """Member-keyed candidates: core may hold them, so a host stop may keep them on disk."""
        self._unsettled: set[str] = set()
        """Live candidates a command may still use; host close must detach, never discard."""
        self._holds: Callable[[], WorkspaceHolds] | None = None
        """What core references now; ``None`` until a run follows core, so members are kept."""
        self._closed = False
        self._close = AsyncCloseOnce()
        self._root_release = AsyncCloseOnce()
        self._evaluations: set[asyncio.Task[object]] = set()
        self.access_fence: Callable[[Path], tuple[str, ...]] | None = None
        self.root = RuntimeWorkspace(self, resources.root)

    @property
    def runner(self) -> BlockingRunner:
        """Where this collection's synchronous workspace effects execute."""
        return self._runner

    def guard_access(self, fenced_by: Callable[[Path], tuple[str, ...]]) -> None:
        """Refuse snapshots of any workspace of this collection while *fenced_by* names a fence.

        Applies to the root, to live candidates, and to candidates opened later.
        """
        self.access_fence = fenced_by
        for handle in (self.root, *self._candidates.values()):
            handle.guard_access(fenced_by)

    @property
    def supports_parallel_candidates(self) -> bool:
        return self._resources.supports_parallel_candidates

    async def create_candidate(
        self,
        from_revision: str | None = None,
        *,
        member_id: str | None = None,
        step_tag: str | None = None,
    ) -> CandidateWorkspace:
        workspace_id = (
            f"s{uuid.uuid4().hex}" if member_id is None else member_workspace_id(member_id)
        )
        async with self._lifecycle_lock:
            if self._closed:
                message = "workspace collection is closed"
                raise RuntimeContractError(message)
            async with self._root_lock:
                revision = from_revision or self.root.revision
                if revision is None:
                    message = "cannot create a candidate without a committed revision"
                    raise RuntimeError(message)
                if not self.supports_parallel_candidates:
                    message = "run environment cannot open isolated candidate sandboxes"
                    raise RuntimeError(message)
                if workspace_id in self._candidates:
                    message = f"member {member_id!r} already has a live candidate workspace"
                    raise RuntimeContractError(message)
                if member_id is not None:
                    unsettled = await run_sync(self._runner, self._resources.unsettled_candidates)
                    if workspace_id in unsettled and workspace_id in await run_sync(
                        self._runner, self._resources.leftover_candidates
                    ):
                        message = (
                            f"member {member_id!r} has an unresolved candidate workspace "
                            "retained by the previous host"
                        )
                        raise RuntimeContractError(message)
                task = asyncio.create_task(
                    self._runner.run(self._open_candidate, workspace_id, revision, step_tag)
                )
                try:
                    resource = await asyncio.shield(task)
                except asyncio.CancelledError as cancelled:
                    await _drain(task)
                    if error := task.exception():
                        cancelled.add_note(f"candidate construction also failed: {error}")
                    else:
                        await run_sync(self._runner, task.result().close)
                    raise
                if member_id is not None:
                    self._members.add(workspace_id)
                return self._register(workspace_id, resource)

    async def create_measured_candidate(
        self, revision: str, *, step_tag: str | None = None
    ) -> CandidateWorkspace:
        """A fresh candidate of *revision* holding exactly what its measured input digests.

        The paths the task declares unread are removed (``WorkspaceResource.withhold_unread``),
        so an evaluation in it reads what ``measured_input`` names, and two revisions with one
        measured input are evaluated on byte-identical trees. A failure discards the candidate.
        """
        candidate = await self.create_candidate(revision, step_tag=step_tag)
        try:
            await run_sync(self._runner, self.resource_for(candidate).withhold_unread)
        except BaseException:
            await candidate.discard()
            raise
        return candidate

    def _open_candidate(
        self, workspace_id: str, revision: str, step_tag: str | None
    ) -> WorkspaceResource:
        """Open the candidate, refusing a revision the run's repository does not hold.

        A revision an agent or a search named is untrusted: it fails as the contract's typed
        error every caller handles, not as the Git failure of the worktree add.
        """
        if not self.resource_for(self.root).has_revision(revision):
            raise WorkspaceRestoreError(revision)
        return self._resources.create_candidate(workspace_id, revision, step_tag)

    def _register(
        self, workspace_id: str, resource: WorkspaceResource
    ) -> RuntimeCandidateWorkspace:
        candidate = RuntimeCandidateWorkspace(self, resource)
        self._candidates[workspace_id] = candidate
        self._candidate_locks[workspace_id] = asyncio.Lock()
        return candidate

    async def reattach_candidate(self, member_id: str) -> RuntimeCandidateWorkspace | None:
        """Reopen the candidate a stopped host left on disk for this member, if any.

        The worktree keeps its content, unlike :meth:`create_candidate`, which
        replaces a leftover directory. Returns the live handle when this process
        already has one, and ``None`` when no worktree exists for the member.
        """
        workspace_id = member_workspace_id(member_id)
        async with self._lifecycle_lock:
            if self._closed:
                message = "workspace collection is closed"
                raise RuntimeContractError(message)
            async with self._root_lock:
                live = self._candidates.get(workspace_id)
                if live is not None:
                    return live
                revision = self.root.revision
                if revision is None:
                    return None
                resource = await run_sync(
                    self._runner, self._resources.reattach_candidate, workspace_id, revision
                )
                if resource is None:
                    return None
                self._members.add(workspace_id)
                return self._register(workspace_id, resource)

    def live_candidate(self, member_id: str) -> RuntimeCandidateWorkspace | None:
        """Return the live candidate of one member, or ``None`` once released."""
        return self._candidates.get(member_workspace_id(member_id))

    async def adopt(
        self, revision: str, *, message: str = "vibesys: adopt revision"
    ) -> AdoptionPublication:
        async with self._root_lock:
            return await run_sync(
                self._runner, self.resource_for(self.root).publish, revision, message
            )

    async def update_checkout(self, publication: AdoptionPublication) -> CheckoutUpdate:
        async with self._root_lock:
            return await run_sync(
                self._runner, self.resource_for(self.root).update_checkout, publication
            )

    async def export_patch(self, revision: str) -> str:
        return await self.root.candidate_patch(revision)

    async def retains(self, revision: str) -> bool:
        return await run_sync(self._runner, self.resource_for(self.root).is_retained, revision)

    def resource_for(self, workspace: Workspace) -> WorkspaceResource:
        if isinstance(workspace, RuntimeWorkspace):
            workspace._ensure_open()  # noqa: SLF001  # lint-waiver: LW-228416 [SLF001]; the owning collection validates its handle lifetime.
        if workspace is self.root:
            return self.root._resource  # noqa: SLF001  # lint-waiver: LW-228406 [SLF001]; the collection resolves its own handle to its private resource.
        if isinstance(workspace, RuntimeCandidateWorkspace):
            current = self._candidates.get(workspace.id or "")
            if current is workspace:
                return workspace._resource  # noqa: SLF001  # lint-waiver: LW-228407 [SLF001]; the collection resolves its own handle to its private resource.
        message = "workspace must be a live handle from this run"
        raise TypeError(message)

    def agent_sandbox_at(self, path: Path) -> CommandRunner | None:
        """Return the sandbox of the live candidate checked out at ``path``.

        ``None`` only for the root workspace: the role's own sandbox serves it,
        so a caller serving several workspaces from one client uses that one
        there. Any other path raises :class:`AgentWorkspaceRouteError` (a released
        or unknown candidate, or one whose environment has no sandbox for it)
        instead of returning ``None``, which the caller would read as "use the
        root container" and fail in a directory that container lacks (#1552).
        """
        if path == self.root.path:
            return None
        for candidate in tuple(self._candidates.values()):
            if candidate.path == path:
                sandbox = self.resource_for(candidate).agent_sandbox()
                if sandbox is None:
                    raise AgentWorkspaceRouteError(path, "its candidate has no agent sandbox")
                return sandbox
        raise AgentWorkspaceRouteError(path, "it is neither the root nor a live candidate")

    def workspace_for(self, workspace: Workspace) -> RuntimeWorkspace:
        """Return one live handle owned by this collection."""
        self.resource_for(workspace)
        if not isinstance(workspace, RuntimeWorkspace):
            message = "workspace must be a live handle from this run"
            raise TypeError(message)
        return workspace

    async def begin_capture(
        self, workspace: Workspace, execution: CaptureExecutionIdentity | None
    ) -> None:
        """Fence a capture in memory, persisting only independently recoverable work."""
        managed = self.workspace_for(workspace)
        workspace_id = managed.id or _ROOT_CAPTURE_WORKSPACE
        if workspace_id in self._unsettled:
            raise CaptureStopUnconfirmedError
        self._unsettled.add(workspace_id)
        if execution is None:
            return
        try:
            await run_sync(
                self._runner,
                self._resources.record_capture_intent,
                workspace_id,
                execution,
            )
        except BaseException as cause:
            error = CaptureStopUnconfirmedError()
            error.add_note(
                "durable capture intent could not be established; the candidate remains retained"
            )
            raise error from cause

    async def finish_capture(self, workspace: Workspace) -> None:
        """Remove in-memory and, when present, durable protection after settlement."""
        managed = self.workspace_for(workspace)
        workspace_id = managed.id or _ROOT_CAPTURE_WORKSPACE
        if self.resource_for(managed).capture_execution is not None:
            try:
                await run_sync(self._runner, self._resources.clear_capture_intent, workspace_id)
            except BaseException as cause:
                error = CaptureStopUnconfirmedError()
                error.add_note(
                    "settlement was observed but its durable capture intent could not be cleared"
                )
                raise error from cause
        self._unsettled.discard(workspace_id)

    async def retain_unsettled(self, workspace: Workspace) -> None:
        """Compatibility entrypoint that establishes durable protection immediately."""
        managed = self.workspace_for(workspace)
        await self.begin_capture(managed, self.resource_for(managed).capture_execution)

    def is_root(self, workspace: Workspace) -> bool:
        self.resource_for(workspace)
        return workspace is self.root

    @asynccontextmanager
    async def _mutation(self, workspace: RuntimeWorkspace) -> AsyncIterator[None]:
        resource = self.resource_for(workspace)
        if resource.id is None:
            async with self._root_lock:
                yield
            return
        lock = self._candidate_locks[resource.id]
        async with lock:
            yield

    async def _evaluate[Result](
        self,
        workspace: RuntimeWorkspace,
        evaluation: Callable[[WorkspaceResource], Awaitable[Result]],
    ) -> Result:
        """Run one trusted evaluation that run teardown cancels.

        The evaluation holds the workspace's mutation lock, so teardown, which
        takes the same lock, would otherwise wait for it to finish on its own:
        on Slurm, for a job that may still be queued. :meth:`begin_close`
        cancels it instead; the cancellation stops its command and job.
        """
        async with self._mutation(workspace):
            if self._closed:
                message = "workspace collection is closed"
                raise RuntimeContractError(message)
            task: asyncio.Task[Result] = asyncio.ensure_future(
                evaluation(self.resource_for(workspace))
            )
            self._evaluations.add(task)
            try:
                return await task
            finally:
                self._evaluations.discard(task)

    async def _discard(self, candidate: RuntimeCandidateWorkspace) -> Released:
        async with self._lifecycle_lock:
            resource = self.resource_for(candidate)
            lock = self._candidate_locks[resource.id or ""]
            async with lock, self._root_lock:
                # A live capture enters this set before its intent is persisted. Persisted-only
                # candidates are refused before reattach, so discard needs no second blocking
                # state read (which could queue ahead of this close on a single-worker runner).
                if resource.id in self._unsettled:
                    message = "candidate workspace has an unresolved capture"
                    raise RuntimeContractError(message)
                steps = ReleaseStack()
                steps.callback(resource.close)
                try:
                    proof = await run_sync(self._runner, steps.close)
                except Exception as error:
                    # The candidate may still exist, so it stays registered and open: a retry
                    # or the run's close tries again. Callers read the failure as a group.
                    message = "candidate workspace cleanup failed"
                    raise ExceptionGroup(message, [error]) from error
                self._forget(resource, candidate)
        return proof

    async def _detach(self, candidate: RuntimeCandidateWorkspace) -> None:
        """Release a member's live handles at a host stop and keep its worktree on disk.

        Core still holds a member's workspace (its attempt is open, its access may be unjudged),
        so the stop of this host is not its release: the next host reattaches it with its work.
        Only a discard, which core asks for when it releases the attempt, removes it.
        """
        async with self._lifecycle_lock:
            resource = self.resource_for(candidate)
            lock = self._candidate_locks[resource.id or ""]
            async with lock, self._root_lock:
                await run_sync(self._runner, resource.detach)
                self._forget(resource, candidate)

    def _forget(self, resource: WorkspaceResource, candidate: RuntimeCandidateWorkspace) -> None:
        """Drop a released or detached candidate and end its handle."""
        self._candidates.pop(resource.id or "", None)
        self._candidate_locks.pop(resource.id or "", None)
        self._members.discard(resource.id or "")
        self._unsettled.discard(resource.id or "")
        candidate._closed = True  # noqa: SLF001  # lint-waiver: LW-228417 [SLF001]; the owning collection terminates its handle after cleanup.

    def follow(self, holds: Callable[[], WorkspaceHolds]) -> None:
        """Decide what outlives this host from what core references, as *holds* reads it.

        From now on :meth:`close` keeps on disk only the member candidates core still holds
        (including unresolved operation scratch) and discards the rest with their worktrees.
        """
        self._holds = holds

    def _kept(self) -> frozenset[str] | None:
        """The candidate workspace IDs a host stop keeps; ``None`` while core is not followed."""
        if self._holds is None:
            return None
        return self._holds().workspace_ids()

    async def release_unheld(self) -> None:
        """Discard every candidate worktree on disk that core no longer references.

        A stopped host left these behind: members of attempts that have since ended, a
        member no later host reattached, scratch candidates of a host that died. Live
        candidates are this host's and stay. Does nothing until :meth:`follow` is called.
        """
        async with self._lifecycle_lock, self._root_lock:
            steps = ReleaseStack()
            self._unsettled = set(
                await run_sync(self._runner, self._resources.reconcile_capture_intents)
            )
            await self._sweep(steps)
            await run_sync(self._runner, steps.close)

    async def _sweep(self, steps: ReleaseStack) -> None:
        """Add to *steps* a removal of each worktree on disk neither core nor this host holds.

        The caller holds the root lock: a removal changes the root's worktree registrations.
        """
        kept = self._kept()
        if kept is None:
            return
        unsettled = await run_sync(self._runner, self._resources.unsettled_candidates)
        for workspace_id in await run_sync(self._runner, self._resources.leftover_candidates):
            if (
                workspace_id not in kept
                and workspace_id not in unsettled
                and workspace_id not in self._candidates
            ):
                steps.callback(self._resources.remove_leftover, workspace_id)

    def begin_close(self) -> None:
        """Reject new work and cancel in-flight trusted evaluations before teardown."""
        if self._closed:
            return
        self._closed = True
        for evaluation in tuple(self._evaluations):
            evaluation.cancel()

    async def close(self) -> None:
        """Close candidates by what core holds, then the root resource.

        A member candidate core still holds, or whose command has an unconfirmed stop, is
        detached: its worktree stays for the next host. Every other candidate is discarded,
        and so is every worktree on disk core no longer references. A final run therefore
        leaves no settled candidate behind, but never removes unresolved remote-command scratch.
        Before :meth:`follow`, core's state is unknown and every member candidate is kept.
        """
        self.begin_close()
        await self._close.run(self._close_once)

    async def _close_once(self) -> Released:
        errors: list[BaseException] = []
        async with self._lifecycle_lock:
            candidates = tuple(self._candidates.values())
        kept = self._kept()
        for candidate in reversed(candidates):
            try:
                if candidate.id in self._unsettled or (
                    candidate.id in self._members and (kept is None or candidate.id in kept)
                ):
                    await self._detach(candidate)
                else:
                    await candidate.discard()
            except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-228410 [BLE001]; run cleanup attempts every candidate in reverse creation order.
                errors.append(error)
        self.root._closed = True  # noqa: SLF001  # lint-waiver: LW-228418 [SLF001]; collection close terminates its root handle with the run.
        try:
            await self._root_release.run(self._close_root)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-228421 [BLE001]; root cleanup joins candidate cleanup failures.
            errors.append(error)
        if errors:
            message = "workspace cleanup failed"
            raise BaseExceptionGroup(message, errors)
        async with self._lifecycle_lock:
            return Released.shown_by(NothingLeft(tuple(self._candidates), "candidate workspaces"))

    async def _close_root(self) -> Released:
        steps = ReleaseStack()
        steps.callback(self.root._resource.close)  # noqa: SLF001  # lint-waiver: LW-228420 [SLF001]; collection close releases the root resource after all candidates.
        async with self._root_lock:
            # Worktrees core no longer references go before the root that registers them.
            await self._sweep(steps)
            return await run_sync(self._runner, steps.close)
