"""Run-scoped issue tracking behind a storage-neutral API."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from vs_issue_tracker.core import IssueBoard
from vs_issue_tracker.github import GitHubIssueTracker
from vs_issue_tracker.progress import FileProgressLog, GitHubProgressLog

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_github.api import GitHubClient
    from vs_issue_tracker.core import Issue, IssueTracker
    from vs_issue_tracker.policy import CreateIssuePolicy
    from vs_issue_tracker.progress import ProgressLog


class IssueTrackerConfig(BaseModel):
    """Serializable selection of one issue persistence provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    backend: Literal["local", "github"] = "local"
    repository: str | None = None

    @classmethod
    def local(cls) -> IssueTrackerConfig:
        """Select filesystem persistence."""
        return cls()

    @classmethod
    def from_backend(
        cls, backend: Literal["local", "github"], *, repository: str | None = None
    ) -> IssueTrackerConfig:
        """Build and validate a provider selection at a composition boundary."""
        return cls(backend=backend, repository=repository)

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
            raise ValueError("repository must use OWNER/REPOSITORY format")  # noqa: TRY003  # lint-waiver: LW-920427 [TRY003]; state the external repository syntax at the provider boundary.
        return value

    @model_validator(mode="after")
    def _validate_backend(self) -> IssueTrackerConfig:
        if self.backend == "github" and self.repository is None:
            raise ValueError("repository is required for the GitHub issue tracker")  # noqa: TRY003  # lint-waiver: LW-920428 [TRY003]; name the remote provider's required configuration.
        if self.backend == "local" and self.repository is not None:
            raise ValueError("repository is only valid for the GitHub issue tracker")  # noqa: TRY003  # lint-waiver: LW-920429 [TRY003]; reject remote configuration for local persistence.
        return self


@dataclass(frozen=True, slots=True)
class IssueToolServer:
    """Agent tool launch data, independent of agent driver packages."""

    name: str
    command: str = "python"
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()


class IssueTrackerSession(Protocol):
    """Issue and progress capabilities opened for one workflow run."""

    @property
    def tracker(self) -> IssueTracker:
        """Return the issue tracker used by orchestration policy."""
        ...

    @property
    def progress(self) -> ProgressLog:
        """Return the append-only run progress log."""
        ...

    def refresh(self) -> list[Issue]:
        """Refresh and publish the current issue view."""
        ...

    def issue_tool_server(self, grant: CreateIssuePolicy) -> IssueToolServer:
        """Expose issue operations under a per-turn creation policy."""
        ...


class _OpenedIssueTrackerSession:
    def __init__(
        self,
        *,
        tracker: IssueTracker,
        progress: ProgressLog,
        tool_target_args: tuple[str, ...],
        view_sink: Callable[[list[Issue]], None] | None,
    ) -> None:
        self._tracker = tracker
        self._progress = progress
        self._tool_target_args = tool_target_args
        self._view_sink = view_sink

    @property
    def tracker(self) -> IssueTracker:
        return self._tracker

    @property
    def progress(self) -> ProgressLog:
        return self._progress

    def refresh(self) -> list[Issue]:
        issues = self._tracker.list()
        if self._view_sink is not None:
            self._view_sink(issues)
        return issues

    def issue_tool_server(self, grant: CreateIssuePolicy) -> IssueToolServer:
        args = [
            "-m",
            "vs_issue_tracker.mcp",
            *self._tool_target_args,
            "--creator",
            grant.creator,
            "--iteration",
            str(grant.iteration),
            "--allowed-types",
            ",".join(sorted(issue_type.value for issue_type in grant.allowed_types)),
        ]
        if grant.cap is not None:
            args.extend(("--cap", str(grant.cap)))
        return IssueToolServer(name="vibesys-issues", args=tuple(args))


def open_issue_tracker_session(  # noqa: PLR0913  # lint-waiver: LW-920430 [PLR0913]; provider-neutral run resources are explicit inputs to the composition factory.
    config: IssueTrackerConfig,
    *,
    local_store_path: Path,
    local_progress_path: Path,
    tool_store_path: str,
    run_id: str,
    view_sink: Callable[[list[Issue]], None] | None = None,
    github_cli: GitHubClient | None = None,
) -> IssueTrackerSession:
    """Open issue and progress persistence selected by ``config``."""
    tracker: IssueTracker
    if config.backend == "local":

        def publish_local_view() -> None:
            if view_sink is not None:
                view_sink(tracker.list())

        tracker = IssueBoard(
            local_store_path,
            on_change=publish_local_view if view_sink is not None else None,
        )
        progress: ProgressLog = FileProgressLog(local_progress_path)
        tool_target_args = (tool_store_path,)
    else:
        repository = cast("str", config.repository)
        tracker = GitHubIssueTracker(repository, cli=github_cli)
        progress = GitHubProgressLog(repository, run_id, cli=github_cli)
        tool_target_args = ("--github-repository", repository)

    return _OpenedIssueTrackerSession(
        tracker=tracker,
        progress=progress,
        tool_target_args=tool_target_args,
        view_sink=view_sink,
    )


__all__ = [
    "IssueToolServer",
    "IssueTrackerConfig",
    "IssueTrackerSession",
    "open_issue_tracker_session",
]
