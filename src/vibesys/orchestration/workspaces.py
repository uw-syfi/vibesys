"""``ctx.workspaces``: root and isolated worktrees, snapshots, transactions, adoption.

Split from ``runtime.py`` by capability; see that module's docstring.
"""

# Capabilities in this module share one private owner for resource lifetime.
# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from vibesys.context import WorkspaceResourceSpec, create_workspace_resources
from vibesys.events import FrameworkSource
from vibesys.runtime import WorkspaceScope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from vibesys.context import _RunResources
    from vibesys.orchestration._host import HostResources


class WorkspaceRestoreError(RuntimeError):
    """A workspace checkout to a retained revision failed.

    Callers that need a hard failure (e.g. role-isolation revert) let this
    propagate. Callers that tolerate a transient checkout failure (e.g.
    hypothesis rollback, R1) catch it, warn, and retry on a later round
    instead of aborting the run.
    """

    def __init__(self, revision: str) -> None:
        """Name the revision that could not be checked out."""
        super().__init__(f"could not restore candidate revision {revision!r}")


class WorkspaceTransactionKeep(Exception):  # noqa: N818  # a signal, not always an error
    """Raise (or subclass) inside a transaction body to skip restore-on-error.

    ``async with workspace.transaction():`` restores the workspace to its
    entry snapshot when the body raises, unless the body called
    ``tx.commit()`` first. Raising a ``WorkspaceTransactionKeep`` (or a
    subclass) instead still propagates the exception but leaves the body's
    mutations on disk, for callers that need the failed state preserved
    (e.g. for diagnostics) rather than rolled back.
    """


class WorkspaceTransaction:
    """A commit flag for one ``WorkspaceHandle.transaction()`` block."""

    def __init__(self, revision: str) -> None:
        """Record the snapshot revision the transaction restores to by default."""
        self.revision = revision
        self._committed = False

    def commit(self) -> None:
        """Keep the current tree; the transaction will not restore on exit."""
        self._committed = True

    @property
    def committed(self) -> bool:
        """Return whether ``commit()`` was called."""
        return self._committed


class WorkspaceHandle:
    """One root or isolated workspace with scoped Git operations."""

    def __init__(self, owner: _Workspaces, scope: WorkspaceScope | None) -> None:
        """Bind a live scope, or the root, to its run workspace manager."""
        self._owner = owner
        self._scope = scope

    @property
    def id(self) -> str | None:
        """Return an isolated scope's identity, if this is a fork."""
        return self._scope.id if self._scope is not None else None

    @property
    def path(self) -> Path:
        """Return this workspace's host path."""
        if self._scope is None:
            return self._owner._host._resources.workspace
        return self._owner._require_scope(self._scope).path

    @property
    def revision(self) -> str | None:
        """Return the retained revision of a fork or current root HEAD."""
        if self._scope is None:
            return self._owner._host._resources.git.current_sha()
        return self._owner._require_scope(self._scope).revision

    @property
    def trusted_input_baseline(self) -> str | None:
        """Return the run's immutable trusted-input Git baseline."""
        return self._owner._host._resources.git.trusted_input_baseline

    async def snapshot(self, label: str) -> str:
        """Commit this workspace's changes and retain its revision."""
        return await self._owner._snapshot(label, scope=self._scope)

    async def restore(
        self,
        revision: str,
        *,
        clean: bool = True,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
    ) -> None:
        """Restore this workspace to a retained revision.

        Declared agent-memory paths (``RunSetup.memory_paths``) are folded
        into *preserve_paths* by default, matching every other restore path
        (``transaction``, ``restore_or_warn``, ``adopt``). Role-isolation
        reverts pass ``preserve_memory=False``: a stray write a role was not
        authorized to make must come back out even when it lands inside a
        declared-memory path, or isolation silently keeps it.
        """
        await self._owner._restore(
            revision,
            scope=self._scope,
            clean=clean,
            preserve_paths=preserve_paths,
            preserve_memory=preserve_memory,
        )

    async def restore_or_warn(
        self,
        revision: str,
        *,
        clean: bool = True,
        preserve_paths: tuple[str, ...] = (),
        round_label: str | None = None,
    ) -> bool:
        """Restore to *revision*, tolerating a transient checkout failure (R1).

        For rollback-style restores that must not abort the run: on success,
        returns ``True``. On a failed checkout, publishes a framework warning
        instead of raising and returns ``False``, so the caller can leave its
        own state unchanged and retry the same restore on a later round.
        """
        try:
            await self.restore(revision, clean=clean, preserve_paths=preserve_paths)
        except WorkspaceRestoreError:
            self._owner._host.warning(
                f"could not restore workspace to revision {revision[:8]}; "
                "will retry on a later round",
                source=FrameworkSource.GIT_TRACKING,
                source_label="rollback",
                round_label=round_label,
            )
            return False
        return True

    async def retain(self, name: str, revision: str) -> str:
        """Keep a revision reachable under a policy-owned name."""
        return await self._owner._retain(name, revision)

    async def pending_changes(self) -> list[str]:
        """List uncommitted candidate changes in this workspace."""
        return await self._owner._pending_changes(scope=self._scope)

    async def candidate_patch(self, revision: str) -> str:
        """Return a candidate diff against the trusted baseline."""
        return await self._owner._candidate_patch(revision, scope=self._scope)

    async def trusted_input_changes(self) -> list[str]:
        """List changed evaluator-owned files."""
        return await self._owner._trusted_input_changes(scope=self._scope)

    async def discard(self) -> None:
        """Close an isolated workspace and all of its agent handles."""
        if self._scope is None:
            raise ValueError("the run root cannot be discarded")  # noqa: TRY003
        await self._owner._discard_scope(self._scope)

    @asynccontextmanager
    async def transaction(
        self, *, preserve: tuple[str, ...] = (), label: str = "transaction"
    ) -> AsyncIterator[WorkspaceTransaction]:
        """Snapshot on entry; restore to it on exit unless the body commits.

        Declared agent memory paths (``RunSetup.memory_paths``) are always
        preserved on the exit restore, in addition to *preserve*. A body
        that calls ``tx.commit()`` keeps whatever tree it leaves behind. A
        body that raises a :class:`WorkspaceTransactionKeep` still
        propagates the exception but skips the restore. Any other exception,
        or simply not committing, restores to the entry snapshot; a failed
        restore raises :class:`WorkspaceRestoreError`.
        """
        revision = await self.snapshot(label)
        tx = WorkspaceTransaction(revision)
        try:
            yield tx
        except WorkspaceTransactionKeep:
            raise
        except BaseException:
            if not tx.committed:
                await self.restore(revision, clean=True, preserve_paths=preserve)
            raise
        else:
            if not tx.committed:
                await self.restore(revision, clean=True, preserve_paths=preserve)


class _Workspaces:
    """Own isolated worktrees and parent adoption for one run."""

    def __init__(self, host: HostResources) -> None:
        self._host = host
        self._scopes: dict[str, WorkspaceScope] = {}
        self._scoped_resources: dict[str, _RunResources] = {}
        self._scope_locks: dict[str, asyncio.Lock] = {}
        self.root = WorkspaceHandle(self, None)

    def _scope_of(self, scope: WorkspaceScope | WorkspaceHandle | None) -> WorkspaceScope | None:
        """Resolve a public handle to its internal scope identity."""
        if isinstance(scope, WorkspaceHandle):
            if scope._owner is not self:
                raise ValueError("workspace handle belongs to another run")  # noqa: TRY003
            return scope._scope
        return scope

    async def fork(self, revision: str | None = None) -> WorkspaceHandle:
        """Open an isolated worktree at a committed parent revision."""
        async with self._host._parent_mutation_lock:
            scope = await self._host._run_blocking(self._fork, revision)
            return WorkspaceHandle(self, scope)

    def _fork(self, revision: str | None) -> WorkspaceScope:
        parent = self._host._resources
        base = revision or parent.git.current_sha()
        if base is None:
            raise RuntimeError("cannot fork a workspace without a committed revision")  # noqa: TRY003
        if not parent.run_environment_view.supports_parallel_candidate_evaluation:
            raise RuntimeError("run environment cannot open isolated candidate sandboxes")  # noqa: TRY003
        scope_id = f"s{uuid.uuid4().hex}"
        context = create_workspace_resources(
            parent,
            WorkspaceResourceSpec(
                scope_id=scope_id,
                revision=base,
                config=self._host.request.config,
                agent_backend=self._host.request.agent_backend,
                cli_provider=self._host.request.cli_provider,
            ),
        )
        scope = WorkspaceScope(id=scope_id, path=context.workspace, revision=base)
        self._scopes[scope_id] = scope
        self._scoped_resources[scope_id] = context
        return scope

    async def _snapshot(
        self, label: str, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> str:
        """Commit candidate edits and retain a fork's revision for later adoption."""
        scope = self._scope_of(scope)
        if scope is None:
            async with self._host._parent_mutation_lock:
                return await self._host._run_blocking(self._snapshot_parent, label)
        lock = self._scope_lock(scope)
        async with lock:
            revision = await self._host._run_blocking(self._snapshot_scoped, label, scope)
            async with self._host._parent_mutation_lock:
                return await self._host._run_blocking(self._retain_scoped, scope, revision)

    def _snapshot_parent(self, label: str) -> str:
        parent = self._host._resources
        parent.git.snapshot(label)
        revision = parent.git.current_sha()
        if revision is None:
            raise RuntimeError("workspace snapshot completed without a Git revision")  # noqa: TRY003
        return revision

    def _snapshot_scoped(self, label: str, scope: WorkspaceScope) -> str:
        current = self._require_scope(scope)
        git = self._scoped_resources[current.id].git
        git.snapshot(label)
        revision = git.current_sha()
        if revision is None:
            raise RuntimeError("workspace snapshot completed without a Git revision")  # noqa: TRY003
        return revision

    def _retain_scoped(self, scope: WorkspaceScope, revision: str) -> str:
        current = self._require_scope(scope)
        self._host._resources.git.retain_candidate(current.id, revision)
        current.revision = revision
        return revision

    def _with_declared_memory(self, preserve_paths: tuple[str, ...]) -> tuple[str, ...]:
        """Merge in the strategy's declared agent memory paths, if any."""
        memory = self._host._setup.memory_paths
        if not memory:
            return preserve_paths
        return tuple(dict.fromkeys((*preserve_paths, *memory)))

    async def adopt(
        self,
        revision: str,
        *,
        clean: bool = True,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
    ) -> None:
        """Materialize a retained candidate revision in the parent workspace."""
        if preserve_memory:
            preserve_paths = self._with_declared_memory(preserve_paths)
        async with self._host._parent_mutation_lock:
            adopted = await self._host._run_blocking(
                self._host._resources.git.checkout_tree,
                revision,
                clean=clean,
                preserve_paths=preserve_paths,
            )
        if not adopted:
            raise WorkspaceRestoreError(revision)

    async def _restore(
        self,
        revision: str,
        *,
        scope: WorkspaceScope | WorkspaceHandle | None = None,
        clean: bool = True,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
    ) -> None:
        """Restore a root or scoped workspace to a committed tree."""
        scope = self._scope_of(scope)
        if scope is None:
            await self.adopt(
                revision,
                clean=clean,
                preserve_paths=preserve_paths,
                preserve_memory=preserve_memory,
            )
            return
        if preserve_memory:
            preserve_paths = self._with_declared_memory(preserve_paths)
        async with self._scope_lock(scope):
            context = self._resources_for(scope)
            restored = await self._host._run_blocking(
                context.git.checkout_tree,
                revision,
                clean=clean,
                preserve_paths=preserve_paths,
            )
            if not restored:
                raise WorkspaceRestoreError(revision)
            scope.revision = revision

    async def _retain(self, name: str, revision: str) -> str:
        """Keep a candidate revision reachable from the parent repository."""
        async with self._host._parent_mutation_lock:
            return await self._host._run_blocking(
                self._host._resources.git.retain_candidate, name, revision
            )

    async def _pending_changes(
        self, *, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> list[str]:
        """List uncommitted changes in one workspace."""
        context = self._resources_for(self._scope_of(scope))
        return await self._host._run_blocking(context.git.pending_changes)

    async def _candidate_patch(
        self, revision: str, *, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> str:
        """Return a candidate patch using this workspace's Git tracker."""
        context = self._resources_for(self._scope_of(scope))
        return await self._host._run_blocking(context.git.candidate_patch, revision)

    async def _trusted_input_changes(
        self, *, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> list[str]:
        """Detect edits to evaluator-owned inputs in one workspace."""
        context = self._resources_for(self._scope_of(scope))
        return await self._host._run_blocking(context.git.trusted_input_changes)

    async def _discard_scope(self, scope: WorkspaceScope | WorkspaceHandle) -> None:
        """Drain scoped agents and gates before removing their worktree."""
        async with self._host._spawn_lock:  # shared lifecycle lock
            scope = self._require_scope(scope)
            errors: list[BaseException] = []
            for agent in tuple(self._host._agents.values()):
                if agent.scope_id != scope.id:
                    continue
                try:
                    await agent.close()
                except BaseException as exc:  # noqa: BLE001  # finish scope cleanup
                    errors.append(exc)
            async with (
                self._host.gates._lock_for(scope),
                self._scope_lock(scope),
                self._host._parent_mutation_lock,
            ):
                try:
                    await self._host._run_blocking(self._discard, scope)
                except BaseException as exc:  # noqa: BLE001  # report all cleanup errors
                    errors.append(exc)
                finally:
                    if scope.id not in self._scopes:
                        self._host.gates._forget(scope)
            if errors:
                raise BaseExceptionGroup("scoped agent cleanup failed", errors)  # noqa: TRY003

    def _discard(self, scope: WorkspaceScope) -> None:
        current = self._require_scope(scope)
        self._scoped_resources[current.id].close()
        del self._scopes[current.id]
        del self._scoped_resources[current.id]
        self._scope_locks.pop(current.id, None)

    def _scope_lock(self, scope: WorkspaceScope) -> asyncio.Lock:
        current = self._require_scope(scope)
        return self._scope_locks.setdefault(current.id, asyncio.Lock())

    def _require_scope(self, scope: WorkspaceScope | WorkspaceHandle) -> WorkspaceScope:
        resolved = self._scope_of(scope)
        if resolved is None:
            raise ValueError("expected an isolated workspace scope")  # noqa: TRY003
        current = self._scopes.get(resolved.id)
        if current is not resolved:
            raise ValueError("workspace scope is closed or belongs to another run")  # noqa: TRY003
        return current

    def _resources_for(self, scope: WorkspaceScope | WorkspaceHandle | None) -> _RunResources:
        """Resolve the sandbox-backed context for a live workspace scope."""
        scope = self._scope_of(scope)
        if scope is None:
            return self._host._resources
        current = self._require_scope(scope)
        return self._scoped_resources[current.id]

    async def close(self) -> None:
        """Discard all remaining worktrees in reverse creation order."""
        errors: list[BaseException] = []
        for scope in reversed(tuple(self._scopes.values())):
            try:
                async with (
                    self._host.gates._lock_for(scope),
                    self._scope_lock(scope),
                    self._host._parent_mutation_lock,
                ):
                    await asyncio.to_thread(self._discard, scope)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("workspace cleanup failed", errors)  # noqa: TRY003
