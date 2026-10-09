"""Shared workspace-access policy for runtime sessions and their fakes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from vs_runtime.contracts import RuntimeContractError, WorkspaceAccess

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def unauthorized_paths(
    changes: list[str],
    allowed: tuple[str, ...],
    *,
    directories: tuple[str, ...] = (),
) -> list[str]:
    """Return changed paths outside the exact-path and directory grants."""
    return [
        path
        for path in changes
        if not any(path == item for item in allowed)
        and not any(path == item or path.startswith(f"{item}/") for item in directories)
    ]


__all__ = [
    "AccessGrant",
    "AccessGuardedWorkspace",
    "WorkspaceAccessRecovery",
    "WorkspaceAccessResult",
    "WorkspaceAccessTarget",
    "enforce_workspace_access",
    "unauthorized_paths",
]


class AccessGrant(BaseModel):
    """What one role may write in a workspace; durable, so a restart enforces the same grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    role_id: str
    access: WorkspaceAccess
    paths: tuple[str, ...] = ()
    """Exact paths a LIMITED grant allows."""
    directories: tuple[str, ...] = ()
    """The subset of ``paths`` that are directories and so also grant their descendants."""


@dataclass(frozen=True)
class WorkspaceAccessResult:
    """Restoration evidence and the remaining uncommitted workspace changes."""

    restored_paths: list[str]
    pending_changes: list[str]


class WorkspaceAccessTarget(Protocol):
    """Workspace effects required to restore an agent's access boundary."""

    async def pending_changes(self) -> list[str]: ...

    async def restore_for_agent(
        self, revision: str, *, preserve_paths: tuple[str, ...]
    ) -> None: ...


class WorkspaceAccessRecovery:
    """Keep failed isolation intent with its workspace until restoration succeeds.

    Every snapshot reconciles this intent first, including snapshots requested
    by another session. A retry cannot turn unauthorized edits into its baseline.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._checkpoint: tuple[str, str, tuple[str, ...], tuple[str, ...]] | None = None
        self._fenced_by: Callable[[], tuple[str, ...]] = tuple

    def guard(self, fenced_by: Callable[[], tuple[str, ...]]) -> None:
        """Refuse snapshots while *fenced_by* names invocations whose writes are not judged.

        The answer comes from durable state, read on every snapshot, so a restarted
        host refuses before any executor has seen the unsettled invocation again.
        """
        self._fenced_by = fenced_by

    def begin(
        self,
        revision: str,
        role_id: str,
        allowed: tuple[str, ...],
        directories: tuple[str, ...],
    ) -> None:
        """Record isolation intent once dispatch settles, before restoration I/O.

        Beginning the same intent again is a retry and changes nothing.
        """
        intent = revision, role_id, allowed, directories
        if self._checkpoint is not None and self._checkpoint != intent:
            message = "workspace access restoration is still pending"
            raise RuntimeContractError(message)
        self._checkpoint = intent

    async def reconcile(
        self,
        workspace: WorkspaceAccessTarget,
        *,
        observer: Callable[[list[str]], None] | None = None,
    ) -> WorkspaceAccessResult:
        """Restore the original baseline or retain its intent on any failure.

        *observer* receives the unauthorized paths before anything is restored, so a
        caller can record the violation durably ahead of the revert. A snapshot goes
        through here, so it is refused while any invocation holds the workspace.
        """
        if fenced := self._fenced_by():
            message = (
                "workspace is fenced: a writer may still run or its writes are not judged "
                f"({', '.join(sorted(fenced))})"
            )
            raise RuntimeContractError(message)
        return await self.settle(workspace, observer=observer)

    async def settle(
        self,
        workspace: WorkspaceAccessTarget,
        *,
        observer: Callable[[list[str]], None] | None = None,
    ) -> WorkspaceAccessResult:
        """Restore the baseline of a writer proven ended; unlike ``reconcile``, ignores fences."""
        async with self._lock:
            return await self._reconcile(workspace, observer)

    async def _reconcile(
        self, workspace: WorkspaceAccessTarget, observer: Callable[[list[str]], None] | None
    ) -> WorkspaceAccessResult:
        if self._checkpoint is None:
            return WorkspaceAccessResult([], [])
        revision, role_id, allowed, directories = self._checkpoint
        changes = await workspace.pending_changes()
        unauthorized = unauthorized_paths(changes, allowed, directories=directories)
        if unauthorized:
            if observer is not None:
                observer(unauthorized)
            await workspace.restore_for_agent(revision, preserve_paths=allowed)
            changes = await workspace.pending_changes()
            remaining = unauthorized_paths(changes, allowed, directories=directories)
            if remaining:
                message = (
                    f"role {role_id!r} left unauthorized workspace changes: {', '.join(remaining)}"
                )
                raise RuntimeContractError(message)
        self._checkpoint = None
        return WorkspaceAccessResult(unauthorized, changes)


class AccessGuardedWorkspace(WorkspaceAccessTarget, Protocol):
    """A workspace whose agent writes are enforced: it snapshots and owns pending recovery."""

    @property
    def access_recovery(self) -> WorkspaceAccessRecovery: ...

    @property
    def path(self) -> Path: ...

    async def snapshot(self, label: str) -> str: ...


async def enforce_workspace_access(
    workspace: AccessGuardedWorkspace,
    grant: AccessGrant,
    baseline: str,
    *,
    observer: Callable[[list[str]], None] | None = None,
) -> WorkspaceAccessResult:
    """Revert what *grant*'s role wrote outside its access since *baseline* was snapshotted.

    The single place that applies the access policy after a turn, shared by the
    legacy session and the session executors. A role with write access is only
    reconciled against intents other sessions left pending. Calling it again after a
    crash or a failed revert resumes the same restoration.
    """
    if grant.access is not WorkspaceAccess.READ_WRITE:
        limited = grant.access is WorkspaceAccess.LIMITED
        workspace.access_recovery.begin(
            baseline,
            grant.role_id,
            grant.paths if limited else (),
            grant.directories if limited else (),
        )
    result = await workspace.access_recovery.settle(workspace, observer=observer)
    if grant.access is WorkspaceAccess.READ_WRITE:
        return WorkspaceAccessResult(result.restored_paths, await workspace.pending_changes())
    return result
