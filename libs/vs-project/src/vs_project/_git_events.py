"""Typed emission interface for Git tracker observations.

The tracker reports what happened; wiring decides where the report goes.
``NullGitTrackerEvents`` discards reports, and callers with other needs (a
core-side event sink, a server-side design log, say) supply their own
implementation of :class:`GitTrackerEvents`.
"""

from __future__ import annotations

from typing import Protocol


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

    def snapshot_recorded(self, label: str, *, commit: str | None) -> None:
        del label, commit

    def baseline_configured(self, commit: str) -> None:
        del commit

    def paths_excluded(self, paths: tuple[str, ...]) -> None:
        del paths

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        del summary, detail
