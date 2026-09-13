"""Typed emission interface for Git tracker observations.

The tracker reports what happened; wiring decides where the report goes.
``CoreGitTrackerEvents`` publishes typed core events on the process-global
output sink, ``NullGitTrackerEvents`` discards them, and callers with other
needs (a server-side design log, say) supply their own implementation.
"""

from __future__ import annotations

from typing import Protocol

from vibesys.render.sink import output_sink
from vibesys.run.events import (
    CoreEventType,
    FrameworkSource,
    WorkspaceSnapshotData,
)


class GitTrackerEvents(Protocol):
    """Observations one Git tracker reports about the tracked workspace."""

    def snapshot_recorded(self, label: str, *, commit: str | None) -> None:
        """A snapshot attempt finished; ``commit`` is None without changes."""
        ...

    def baseline_configured(self, commit: str) -> None:
        """The trusted-input baseline resolved to ``commit``."""
        ...

    def paths_excluded(self, paths: tuple[str, ...]) -> None:
        """Unreadable ``paths`` were excluded from future snapshots."""
        ...

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        """A non-fatal Git operation fault an operator should see."""
        ...


class NullGitTrackerEvents:
    """Discard tracker observations (tests, internal resume helpers)."""

    def snapshot_recorded(self, label: str, *, commit: str | None) -> None:  # noqa: D102
        del label, commit

    def baseline_configured(self, commit: str) -> None:  # noqa: D102
        del commit

    def paths_excluded(self, paths: tuple[str, ...]) -> None:  # noqa: D102
        del paths

    def warning(self, summary: str, *, detail: str | None = None) -> None:  # noqa: D102
        del summary, detail


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
