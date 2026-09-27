"""Typed emission interface for Git tracker observations.

``GitTrackerEvents`` and ``NullGitTrackerEvents`` now live in ``vs_project``
and are re-exported here so core-internal importers keep working.
``CoreGitTrackerEvents`` stays in core: it projects tracker callbacks onto the
owning run's semantic event stream, which ``vs_project`` cannot depend on.
"""

from __future__ import annotations

from vibesys.events import (
    CoreEventType,
    CoreEventWriter,
    FrameworkSource,
    FrameworkWarningData,
    WorkspaceSnapshotData,
)
from vs_project.api import GitTrackerEvents, NullGitTrackerEvents

__all__ = [
    "CoreGitTrackerEvents",
    "GitTrackerEvents",
    "NullGitTrackerEvents",
]


class CoreGitTrackerEvents:
    """Publish tracker observations on one run's semantic event stream."""

    def __init__(self, events: CoreEventWriter) -> None:
        """Route tracker observations into ``events``."""
        self._events = events

    def snapshot_recorded(self, label: str, *, commit: str | None) -> None:
        """Publish a recorded workspace snapshot event."""
        self._events.emit(
            CoreEventType.WORKSPACE_SNAPSHOT,
            data=WorkspaceSnapshotData(label=label, commit=commit),
        )

    def baseline_configured(self, commit: str) -> None:
        """Publish the selected baseline commit as a workspace event."""
        self._events.emit(
            CoreEventType.WORKSPACE_SNAPSHOT,
            data=WorkspaceSnapshotData(baseline=commit),
        )

    def paths_excluded(self, paths: tuple[str, ...]) -> None:
        """Publish the workspace paths excluded from source tracking."""
        self._events.emit(
            CoreEventType.WORKSPACE_SNAPSHOT,
            data=WorkspaceSnapshotData(excluded_paths=paths),
        )

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        """Publish a Git-tracking warning through the framework event sink."""
        self._events.emit(
            CoreEventType.FRAMEWORK_WARNING,
            data=FrameworkWarningData(
                summary=summary,
                detail=detail,
                source=FrameworkSource.GIT_TRACKING,
            ),
        )
