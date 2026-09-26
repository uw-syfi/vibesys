"""Public issue-tracker session and tool-grant contract tests."""

from pathlib import Path

from vs_issue_tracker.api import (
    CreateIssuePolicy,
    IssueTrackerSession,
    IssueType,
    open_local_issue_tracker_session,
)


def test_local_session_unifies_tracker_progress_and_tool_grant(tmp_path: Path) -> None:
    session: IssueTrackerSession = open_local_issue_tracker_session(
        store_path=tmp_path / "issues.json",
        progress_path=tmp_path / "progress.md",
        tool_store_path="issues.json",
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
    session = open_local_issue_tracker_session(
        store_path=tmp_path / "issues.json",
        progress_path=tmp_path / "progress.md",
        tool_store_path="issues.json",
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
    assert published == [[issue]]
