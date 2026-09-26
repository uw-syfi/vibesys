"""Public issue-tracker session and tool-grant contract tests."""

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from vs_issue_tracker.api import (
    CreateIssuePolicy,
    IssueTrackerConfig,
    IssueTrackerSession,
    IssueType,
    open_issue_tracker_session,
)

if TYPE_CHECKING:
    from vs_github.api import GitHubClient


class _OpeningGitHubClient:
    """Minimum stateful fake needed to open a remote tracker session."""

    def __init__(self) -> None:
        self.labels: list[tuple[str, str]] = []
        self.created: list[tuple[str, str, str, tuple[str, ...]]] = []

    def ensure_authenticated(self) -> None:
        pass

    def ensure_label(self, repository: str, name: str, *, color: str = "ededed") -> None:
        del color
        self.labels.append((repository, name))

    def list_issues(self, _repository: str) -> list[dict[str, object]]:
        return []

    def create_issue(self, repository: str, *, title: str, body: str, labels: Sequence[str]) -> int:
        self.created.append((repository, title, body, tuple(labels)))
        return 7


def test_local_session_unifies_tracker_progress_and_tool_grant(tmp_path: Path) -> None:
    session: IssueTrackerSession = open_issue_tracker_session(
        IssueTrackerConfig.local(),
        local_store_path=tmp_path / "issues.json",
        local_progress_path=tmp_path / "progress.md",
        tool_store_path="issues.json",
        run_id="run-1",
    )
    issue = session.tracker.create(
        type=IssueType.FEATURE,
        title="initial task",
        description="do work",
        created_by="loop:bootstrap",
        iteration=1,
    )
    session.progress.append("## Iter 1\n\nCompleted.\n\n")

    grant = CreateIssuePolicy(
        creator="judge",
        iteration=2,
        cap=1,
        allowed_types=frozenset({IssueType.BUG}),
    )
    server = session.issue_tool_server(grant)

    assert session.tracker.get(issue.id) == issue
    assert session.progress.read() == ("# Experiment Progress\n\n## Iter 1\n\nCompleted.\n\n")
    assert server.name == "vibesys-issues"
    assert server.command == "python"
    assert server.env == ()
    assert server.args == (
        "-m",
        "vs_issue_tracker.mcp",
        "issues.json",
        "--creator",
        "judge",
        "--iteration",
        "2",
        "--allowed-types",
        "bug",
        "--cap",
        "1",
    )


def test_refresh_publishes_current_issue_snapshot_to_view_sink(tmp_path: Path) -> None:
    published = []
    session = open_issue_tracker_session(
        IssueTrackerConfig.local(),
        local_store_path=tmp_path / "issues.json",
        local_progress_path=tmp_path / "progress.md",
        tool_store_path="issues.json",
        run_id="run-1",
        view_sink=published.append,
    )
    issue = session.tracker.create(
        type=IssueType.BUG,
        title="broken behavior",
        description="details",
        created_by="agent",
        iteration=1,
    )

    snapshot = session.refresh()

    assert snapshot == [issue]
    assert published == [[issue], [issue]]


def test_remote_session_owns_provider_wiring_and_run_identity(tmp_path: Path) -> None:
    github = _OpeningGitHubClient()
    session = open_issue_tracker_session(
        IssueTrackerConfig.from_backend("github", repository="owner/repo"),
        local_store_path=tmp_path / "unused.json",
        local_progress_path=tmp_path / "unused.md",
        tool_store_path="unused.json",
        run_id="canonical-run-id",
        github_cli=cast("GitHubClient", github),
    )

    assert github.created[0][0:2] == (
        "owner/repo",
        "VibeSys progress: canonical-run-id",
    )
    server = session.issue_tool_server(
        CreateIssuePolicy(
            creator="judge",
            iteration=3,
            cap=1,
            allowed_types=frozenset({IssueType.BUG}),
        )
    )
    assert server.args[:4] == (
        "-m",
        "vs_issue_tracker.mcp",
        "--github-repository",
        "owner/repo",
    )
