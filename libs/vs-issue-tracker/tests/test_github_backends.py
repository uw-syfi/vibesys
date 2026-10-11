"""Contract tests for GitHub-backed issue and progress stores."""

from __future__ import annotations

import pytest

from vs_github.api import FakeGitHubClient
from vs_issue_tracker.api import (
    GitHubIssueTracker,
    GitHubProgressLog,
    IssueStatus,
    IssueTrackerLoadError,
    IssueType,
)


def test_github_issue_tracker_preserves_status_attempts_and_history() -> None:
    cli = FakeGitHubClient()
    tracker = GitHubIssueTracker("owner/repo", cli=cli)
    issue = tracker.create(
        type=IssueType.BUG,
        title="Keep my body",
        description="User authored description",
        created_by="perf_eval",
        iteration=2,
    )
    assert issue.id == 1
    assert issue.history[0].action == "create"

    tracker.increment_attempts(issue.id, actor="loop", iteration=2)
    tracker.update_status(
        issue.id,
        IssueStatus.BLOCKED,
        actor="loop",
        iteration=2,
        note="attempt budget exhausted",
    )
    blocked = tracker.get(issue.id)
    assert blocked is not None
    assert blocked.status is IssueStatus.BLOCKED
    assert blocked.attempts == 1
    assert blocked.description == "User authored description"
    assert [event.action for event in blocked.history] == [
        "create",
        "attempt",
        "open->blocked",
    ]

    assert tracker.reopen_blocked(actor="operator", iteration=3) == [issue.id]
    reopened = tracker.get(issue.id)
    assert reopened is not None
    assert reopened.status is IssueStatus.OPEN
    assert reopened.attempts == 0
    assert reopened.history[-1].action == "blocked->open"


def test_progress_log_uses_separate_issue_and_is_hidden_from_tracker() -> None:
    cli = FakeGitHubClient()
    progress = GitHubProgressLog("owner/repo", "run-123", cli=cli)
    progress.append("## Iter 1\n\nA completed turn.\n\n")
    assert progress.read() == "# Experiment Progress\n\n## Iter 1\n\nA completed turn.\n\n"

    tracker = GitHubIssueTracker("owner/repo", cli=cli)
    assert tracker.list() == []


def test_reserved_issue_metadata_corruption_is_reported() -> None:
    cli = FakeGitHubClient()
    tracker = GitHubIssueTracker("owner/repo", cli=cli)
    issue = tracker.create(
        type=IssueType.BUG,
        title="Broken metadata",
        description="body",
        created_by="judge",
        iteration=1,
    )
    cli.issues[issue.id]["comments"].append(
        {"body": '<!-- vibesys:issue-event:v1\n{"action": invalid}\n-->'}
    )

    with pytest.raises(IssueTrackerLoadError, match="event metadata is invalid"):
        tracker.get(issue.id)


def test_reserved_progress_metadata_corruption_is_reported() -> None:
    cli = FakeGitHubClient()
    progress = GitHubProgressLog("owner/repo", "run-123", cli=cli)
    cli.issues[1]["comments"].append({"body": "<!-- vibesys:progress-entry:v1\nnot-json\n-->"})

    with pytest.raises(IssueTrackerLoadError, match="progress metadata is invalid"):
        progress.read()
