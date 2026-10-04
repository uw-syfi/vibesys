"""Shared workspace-access policy for runtime sessions and their fakes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from vs_runtime.contracts import RuntimeContractError


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
    "WorkspaceAccessRecovery",
    "WorkspaceAccessResult",
    "WorkspaceAccessTarget",
    "unauthorized_paths",
]


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

    def begin(
        self,
        revision: str,
        role_id: str,
        allowed: tuple[str, ...],
        directories: tuple[str, ...],
    ) -> None:
        """Record isolation intent once dispatch settles, before restoration I/O."""
        if self._checkpoint is not None:
            message = "workspace access restoration is still pending"
            raise RuntimeContractError(message)
        self._checkpoint = revision, role_id, allowed, directories

    async def reconcile(self, workspace: WorkspaceAccessTarget) -> WorkspaceAccessResult:
        """Restore the original baseline or retain its intent on any failure."""
        async with self._lock:
            return await self._reconcile(workspace)

    async def _reconcile(self, workspace: WorkspaceAccessTarget) -> WorkspaceAccessResult:
        if self._checkpoint is None:
            return WorkspaceAccessResult([], [])
        revision, role_id, allowed, directories = self._checkpoint
        changes = await workspace.pending_changes()
        unauthorized = unauthorized_paths(changes, allowed, directories=directories)
        if unauthorized:
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
