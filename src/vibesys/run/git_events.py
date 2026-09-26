"""Typed emission interface for Git tracker observations.

``GitTrackerEvents`` and ``NullGitTrackerEvents`` now live in ``vs_project``
and are re-exported here so core-internal importers keep working.
``CoreGitTrackerEvents`` stays in core: it publishes typed core events on the
process-global output sink, which ``vs_project`` (a leaf library) cannot
depend on.
"""

from __future__ import annotations

from framework.api import GitTrackerEvents, NullGitTrackerEvents
from vibesys.events import (
    CoreEventType,
    FrameworkSource,
    WorkspaceSnapshotData,
)
from vibesys.render.sink import output_sink

__all__ = [
    "CoreGitTrackerEvents",
    "GitTrackerEvents",
    "NullGitTrackerEvents",
]


class CoreGitTrackerEvents:
    """Publish tracker observations as typed core events on the output sink."""

    def snapshot_recorded(self, label: str, *, commit: str | None) -> None:
        """Publish a recorded workspace snapshot event."""
        output_sink().emit(
            CoreEventType.WORKSPACE_SNAPSHOT,
            data=WorkspaceSnapshotData(label=label, commit=commit),
        )

    def baseline_configured(self, commit: str) -> None:
        """Publish the selected baseline commit as a workspace event."""
        output_sink().emit(
            CoreEventType.WORKSPACE_SNAPSHOT,
            data=WorkspaceSnapshotData(baseline=commit),
        )

    def paths_excluded(self, paths: tuple[str, ...]) -> None:
        """Publish the workspace paths excluded from source tracking."""
        output_sink().emit(
            CoreEventType.WORKSPACE_SNAPSHOT,
            data=WorkspaceSnapshotData(excluded_paths=paths),
        )

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        """Publish a Git-tracking warning through the framework event sink."""
        output_sink().framework_warning(
            summary,
            detail=detail,
            source=FrameworkSource.GIT_TRACKING,
        )
