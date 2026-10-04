"""Private run-owned workspace collection and resource lifetime."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Protocol

from vs_runtime._workspace_access import WorkspaceAccessRecovery
from vs_runtime.contracts import (
    RuntimeContractError,
    WorkspaceRestoreError,
    Workspaces,
    member_workspace_id,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from vs_runtime._agent_execution import AgentExecutionScope
    from vs_runtime._agent_sessions import RuntimeWorkspaceAgentSessions
    from vs_runtime._trusted_evaluation import TrustedAccuracyResult, TrustedBenchmarkResult
    from vs_runtime._workspace_runtime import CommandExecutionResult, WorkspaceEvaluationSpec
    from vs_runtime.contracts import CandidateWorkspace, Workspace


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

    def restore(
        self,
        revision: str,
        *,
        clean: bool,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
    ) -> bool: ...

    def try_restore(self, revision: str, *, clean: bool) -> bool: ...

    def retain(self, revision: str, reference: str) -> None: ...

    def has_revision(self, revision: str) -> bool: ...

    def matches_revision(self, revision: str) -> bool: ...

    def find_snapshot(self, label: str) -> str | None: ...

    def pending_changes(self) -> list[str]: ...

    def candidate_patch(self, revision: str) -> str: ...

    def trusted_input_changes(self) -> list[str]: ...

    def is_directory(self, path: str) -> bool: ...

    def execute(self, command: str, timeout_seconds: int | None) -> CommandExecutionResult: ...

    def agent_scope(self) -> AgentExecutionScope: ...

    @property
    def evaluation_spec(self) -> WorkspaceEvaluationSpec: ...

    async def trusted_accuracy(self, command_override: str | None) -> TrustedAccuracyResult: ...

    async def trusted_benchmark(
        self,
        command_override: str | None,
        required_metrics: frozenset[str],
    ) -> TrustedBenchmarkResult: ...

    def close(self) -> None: ...


class WorkspaceResourceProvider(Protocol):
    """One coherent source of root and candidate workspace resources."""

    @property
    def root(self) -> WorkspaceResource: ...

    @property
    def supports_parallel_candidates(self) -> bool: ...

    def create_candidate(self, workspace_id: str, revision: str, /) -> WorkspaceResource: ...

    def reattach_candidate(self, workspace_id: str, revision: str, /) -> WorkspaceResource | None:
        """Reopen a candidate worktree a stopped host left on disk, keeping its content."""
        ...


class OwnedWorkspaces(Workspaces, Protocol):
    """Composition lifetime for a runtime-owned workspace collection."""

    def begin_close(self) -> None:
        """Reject new work before teardown begins."""

    async def close(self) -> None:
        """Release sessions and workspaces in dependency order."""
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
    operation: Callable[P, Result],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> Result:
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
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
        self._closed = False

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
            return await run_sync(self._resource.snapshot, label)

    async def restore(self, revision: str, *, clean: bool = True) -> None:
        await self._restore(revision, clean=clean)

    async def _restore(
        self,
        revision: str,
        *,
        clean: bool,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
    ) -> None:
        async with self._owner._mutation(self):  # noqa: SLF001  # lint-waiver: LW-228403 [SLF001]; a workspace handle delegates synchronization to its owning collection.
            restored = await run_sync(
                self._resource.restore,
                revision,
                clean=clean,
                preserve_paths=preserve_paths,
                preserve_memory=preserve_memory,
            )
        if not restored:
            raise WorkspaceRestoreError(revision)

    async def try_restore(self, revision: str, *, clean: bool = True) -> bool:
        async with self._owner._mutation(self):  # noqa: SLF001  # lint-waiver: LW-228411 [SLF001]; a workspace handle delegates synchronization to its owning collection.
            return await run_sync(self._resource.try_restore, revision, clean=clean)

    async def retain(self, revision: str, *, label: str) -> None:
        if not label:
            message = "workspace retention label must be nonempty"
            raise ValueError(message)
        digest = hashlib.sha256(f"{label}\0{revision}".encode()).hexdigest()
        async with self._owner._root_lock:  # noqa: SLF001  # lint-waiver: LW-228404 [SLF001]; retention mutates the collection's shared root Git metadata.
            self._ensure_open()
            await run_sync(self._resource.retain, revision, f"retained-{digest}")

    async def snapshot_and_retain(self, label: str, *, retention_label: str) -> str:
        revision = await self.snapshot(label)
        await self.retain(revision, label=retention_label)
        return revision

    async def has_revision(self, revision: str) -> bool:
        self._ensure_open()
        return await run_sync(self._resource.has_revision, revision)

    async def matches_revision(self, revision: str) -> bool:
        self._ensure_open()
        async with self._owner._mutation(self):  # noqa: SLF001  # lint-waiver: LW-402310 [SLF001]; a workspace handle delegates synchronization to its owning collection.
            return await run_sync(self._resource.matches_revision, revision)

    async def find_snapshot(self, label: str) -> str | None:
        self._ensure_open()
        async with self._owner._mutation(self):  # noqa: SLF001  # lint-waiver: LW-402311 [SLF001]; a workspace handle delegates synchronization to its owning collection.
            return await run_sync(self._resource.find_snapshot, label)

    async def pending_changes(self) -> list[str]:
        self._ensure_open()
        return await run_sync(self._resource.pending_changes)

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
        return await run_sync(self._resource.candidate_patch, revision)

    async def trusted_input_changes(self) -> list[str]:
        return await run_sync(self._resource.trusted_input_changes)


class RuntimeCandidateWorkspace(RuntimeWorkspace):
    """One isolated candidate with idempotent early release."""

    def __init__(self, owner: RuntimeWorkspaces, resource: WorkspaceResource) -> None:
        super().__init__(owner, resource)
        self._discard_task: asyncio.Task[None] | None = None

    async def discard(self) -> None:
        if self._discard_task is None:
            self._discard_task = asyncio.create_task(self._owner._discard(self))  # noqa: SLF001  # lint-waiver: LW-228405 [SLF001]; candidate release is owned by its creating collection.
        await asyncio.shield(self._discard_task)


class RuntimeWorkspaces:
    """Own workspace handles, agent sessions, and their teardown order."""

    def __init__(
        self,
        resources: WorkspaceResourceProvider,
    ) -> None:
        self._resources = resources
        self._root_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._candidate_locks: dict[str, asyncio.Lock] = {}
        self._candidates: dict[str, RuntimeCandidateWorkspace] = {}
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._sessions: RuntimeWorkspaceAgentSessions | None = None
        self._evaluations: set[asyncio.Task[object]] = set()
        self.root = RuntimeWorkspace(self, resources.root)

    def _attach_sessions(self, sessions: RuntimeWorkspaceAgentSessions) -> None:
        """Complete the private ownership cycle during runtime construction."""
        if self._sessions is not None:
            message = "agent sessions are already attached"
            raise RuntimeError(message)
        self._sessions = sessions

    def _owned_sessions(self) -> RuntimeWorkspaceAgentSessions:
        sessions = self._sessions
        if sessions is None:
            message = "runtime workspace construction is incomplete"
            raise RuntimeError(message)
        return sessions

    @property
    def supports_parallel_candidates(self) -> bool:
        return self._resources.supports_parallel_candidates

    async def create_candidate(
        self,
        from_revision: str | None = None,
        *,
        member_id: str | None = None,
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
                task = asyncio.create_task(
                    asyncio.to_thread(self._resources.create_candidate, workspace_id, revision)
                )
                try:
                    resource = await asyncio.shield(task)
                except asyncio.CancelledError as cancelled:
                    await _drain(task)
                    if error := task.exception():
                        cancelled.add_note(f"candidate construction also failed: {error}")
                    else:
                        await run_sync(task.result().close)
                    raise
                return self._register(workspace_id, resource)

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
                    self._resources.reattach_candidate, workspace_id, revision
                )
                if resource is None:
                    return None
                return self._register(workspace_id, resource)

    def live_candidate(self, member_id: str) -> RuntimeCandidateWorkspace | None:
        """Return the live candidate of one member, or ``None`` once released."""
        return self._candidates.get(member_workspace_id(member_id))

    async def adopt(self, revision: str) -> None:
        await self.root.restore(revision)

    async def export_patch(self, revision: str) -> str:
        return await self.root.candidate_patch(revision)

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

    def workspace_for(self, workspace: Workspace) -> RuntimeWorkspace:
        """Return one live handle owned by this collection."""
        self.resource_for(workspace)
        if not isinstance(workspace, RuntimeWorkspace):
            message = "workspace must be a live handle from this run"
            raise TypeError(message)
        return workspace

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

    async def _discard(self, candidate: RuntimeCandidateWorkspace) -> None:
        async with self._lifecycle_lock:
            resource = self.resource_for(candidate)
            errors: list[BaseException] = []
            try:
                await self._owned_sessions().close_workspace(candidate)
            except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-228408 [BLE001]; candidate resource cleanup must continue after session cleanup fails.
                errors.append(error)
            lock = self._candidate_locks[resource.id or ""]
            async with lock, self._root_lock:
                try:
                    await run_sync(resource.close)
                except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-228409 [BLE001]; all candidate bookkeeping must close even when one resource close fails.
                    errors.append(error)
                finally:
                    self._candidates.pop(resource.id or "", None)
                    self._candidate_locks.pop(resource.id or "", None)
                    candidate._closed = True  # noqa: SLF001  # lint-waiver: LW-228417 [SLF001]; the owning collection terminates its handle after cleanup.
            if errors:
                message = "candidate workspace cleanup failed"
                raise BaseExceptionGroup(message, errors)

    def begin_close(self) -> None:
        """Reject new work and cancel in-flight trusted evaluations before teardown."""
        if self._closed:
            return
        self._closed = True
        for evaluation in tuple(self._evaluations):
            evaluation.cancel()
        self._owned_sessions().begin_close()

    async def close(self) -> None:
        """Close candidate pairs, root sessions, then the root resource."""
        if self._close_task is None:
            self.begin_close()
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        errors: list[BaseException] = []
        async with self._lifecycle_lock:
            candidates = tuple(self._candidates.values())
        for candidate in reversed(candidates):
            try:
                await candidate.discard()
            except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-228410 [BLE001]; run cleanup attempts every candidate in reverse creation order.
                errors.append(error)
        try:
            await self._owned_sessions().close()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-228422 [BLE001]; root sessions must not prevent root resource cleanup.
            errors.append(error)
        async with self._root_lock:
            try:
                await run_sync(self.root._resource.close)  # noqa: SLF001  # lint-waiver: LW-228420 [SLF001]; collection close releases the root resource after all candidates.
            except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-228421 [BLE001]; root cleanup joins candidate cleanup failures.
                errors.append(error)
        self.root._closed = True  # noqa: SLF001  # lint-waiver: LW-228418 [SLF001]; collection close terminates its root handle with the run.
        if errors:
            message = "workspace cleanup failed"
            raise BaseExceptionGroup(message, errors)
