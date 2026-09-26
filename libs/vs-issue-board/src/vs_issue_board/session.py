"""Issue-tracker session and agent-tool launch contracts.

The session keeps issue and progress persistence together and owns the
provider-neutral description of the issue tools subprocess. The agent runtime
adapts :class:`IssueToolServer` to its driver's launch contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_issue_board.core import IssueBoard
from vs_issue_board.progress import FileProgressLog, ProgressLog

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_issue_board.core import Issue, IssueTracker
    from vs_issue_board.policy import CreateIssuePolicy


@dataclass(frozen=True, slots=True)
class IssueToolServer:
    """Stdio launch data for issue tools, independent of agent-driver packages."""

    name: str
    command: str = "python"
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()


class IssueTrackerSession(Protocol):
    """Issue and progress capabilities opened for one workflow run."""

    @property
    def tracker(self) -> IssueTracker:
        """Return the storage-neutral issue tracker for loop operations."""
        ...

    @property
    def progress(self) -> ProgressLog:
        """Return the storage-neutral run progress log."""
        ...

    def refresh(self) -> list[Issue]:
        """Refresh the issue view and publish it to the configured view sink."""
        ...

    def issue_tool_server(self, grant: CreateIssuePolicy) -> IssueToolServer:
        """Describe an issue tool server enforcing the supplied per-turn grant."""
        ...


class LocalIssueTrackerSession:
    """Session over local JSON issues and a local Markdown progress log."""

    def __init__(
        self,
        *,
        tracker: IssueTracker,
        progress: ProgressLog,
        tool_store_path: str,
        view_sink: Callable[[list[Issue]], None] | None = None,
    ) -> None:
        """Bind local persistence with the path visible to an agent subprocess."""
        self._tracker = tracker
        self._progress = progress
        self._tool_store_path = tool_store_path
        self._view_sink = view_sink

    @property
    def tracker(self) -> IssueTracker:
        """Return the session's issue tracker."""
        return self._tracker

    @property
    def progress(self) -> ProgressLog:
        """Return the session's progress log."""
        return self._progress

    def refresh(self) -> list[Issue]:
        """Read the current issues and publish the resulting view if configured."""
        issues = self._tracker.list()
        if self._view_sink is not None:
            self._view_sink(issues)
        return issues

    def issue_tool_server(self, grant: CreateIssuePolicy) -> IssueToolServer:
        """Describe a local issue server configured with the per-turn grant."""
        args = [
            "-m",
            "vs_issue_board.mcp",
            self._tool_store_path,
            "--creator",
            grant.creator,
            "--iteration",
            str(grant.iteration),
            "--allowed-types",
            ",".join(sorted(issue_type.value for issue_type in grant.allowed_types)),
        ]
        if grant.cap is not None:
            args.extend(("--cap", str(grant.cap)))
        return IssueToolServer(
            name="vibesys-issues",
            args=tuple(args),
        )


def open_local_issue_tracker_session(
    *,
    store_path: Path,
    progress_path: Path,
    tool_store_path: str,
    on_change: Callable[[], None] | None = None,
    view_sink: Callable[[list[Issue]], None] | None = None,
) -> IssueTrackerSession:
    """Open local issue and progress persistence as one workflow session.

    ``tool_store_path`` is the path as seen from the agent subprocess working
    directory. ``on_change`` is forwarded to :class:`IssueBoard` for local
    derived views. ``view_sink`` receives current issue snapshots whenever
    the session is explicitly refreshed, including after a separate tool
    server process writes to the store.
    """
    tracker = IssueBoard(store_path, on_change=on_change)
    progress = FileProgressLog(progress_path)
    return LocalIssueTrackerSession(
        tracker=tracker,
        progress=progress,
        tool_store_path=tool_store_path,
        view_sink=view_sink,
    )


__all__ = [
    "IssueToolServer",
    "IssueTrackerSession",
    "LocalIssueTrackerSession",
    "open_local_issue_tracker_session",
]
