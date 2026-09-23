"""Typed emission interface for Git tracker observations.

``GitTrackerEvents`` and ``NullGitTrackerEvents`` now live in ``vs_project``
and are re-exported here so core-internal importers keep working.
``CoreGitTrackerEvents`` stays in core: it publishes typed core events on the
process-global output sink, which ``vs_project`` (a leaf library) cannot
depend on.
"""

from __future__ import annotations

from vibesys.events import (
    CoreEventType,
    FrameworkSource,
    WorkspaceSnapshotData,
)
from vibesys.render.sink import output_sink
from vs_project.api import GitTrackerEvents, NullGitTrackerEvents

__all__ = [
    "CoreGitTrackerEvents",
    "GitTrackerEvents",
    "NullGitTrackerEvents",
]


class CoreGitTrackerEvents:
    """Publish tracker observations as typed core events on the output sink."""

    def snapshot_recorded(self, label: str, *, commit: str | None) -> None:  # noqa: D102
        output_sink().emit(
            CoreEventType.WORKSPACE_SNAPSHOT,
            data=WorkspaceSnapshotData(label=label, commit=commit),
        )

    def baseline_configured(self, commit: str) -> None:  # noqa: D102
        output_sink().emit(
            CoreEventType.WORKSPACE_SNAPSHOT,
            data=WorkspaceSnapshotData(baseline=commit),
        )

    def paths_excluded(self, paths: tuple[str, ...]) -> None:  # noqa: D102
        output_sink().emit(
            CoreEventType.WORKSPACE_SNAPSHOT,
            data=WorkspaceSnapshotData(excluded_paths=paths),
        )

    def warning(self, summary: str, *, detail: str | None = None) -> None:  # noqa: D102
        output_sink().framework_warning(
            summary,
            detail=detail,
            source=FrameworkSource.GIT_TRACKING,
        )
