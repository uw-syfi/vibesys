"""Contract tests for GitHub-backed issue and progress stores."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_issue_tracker.api import (
    GitHubIssueTracker,
    GitHubProgressLog,
    IssueStatus,
    IssueType,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class MemoryGitHubCLI:
    def __init__(self) -> None:
        self.issues: dict[int, dict[str, object]] = {}
        self.next_number = 1
        self.labels: set[str] = set()

    def ensure_authenticated(self) -> None:
        pass

    def ensure_label(self, _repository: str, name: str, *, color: str = "ededed") -> None:
        del color
        self.labels.add(name)

    def list_issues(self, _repository: str) -> list[dict[str, object]]:
        return [dict(issue) for issue in self.issues.values()]

    def view_issue(self, _repository: str, issue_number: int) -> dict[str, object]:
        return dict(self.issues[issue_number])

    def create_issue(
        self,
        _repository: str,
        *,
        title: str,
        body: str,
        labels: Sequence[str],
    ) -> int:
        number = self.next_number
        self.next_number += 1
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "state": "OPEN",
            "labels": [{"name": label} for label in labels],
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "author": {"login": "octocat"},
            "comments": [],
        }
        return number

    def edit_issue(
        self,
        _repository: str,
        issue_number: int,
        *,
        title: str | None = None,
        add_labels: Sequence[str] = (),
        remove_labels: Sequence[str] = (),
    ) -> None:
        issue = self.issues[issue_number]
        if title is not None:
            issue["title"] = title
        labels = {item["name"] for item in issue["labels"]}
        labels.difference_update(remove_labels)
        labels.update(add_labels)
        issue["labels"] = [{"name": label} for label in sorted(labels)]

    def comment_issue(self, _repository: str, issue_number: int, body: str) -> None:
        self.issues[issue_number]["comments"].append({"body": body})

    def set_issue_state(self, _repository: str, issue_number: int, *, issue_open: bool) -> None:
        self.issues[issue_number]["state"] = "OPEN" if issue_open else "CLOSED"


def test_github_issue_tracker_preserves_status_attempts_and_history() -> None:
    cli = MemoryGitHubCLI()
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
    cli = MemoryGitHubCLI()
    progress = GitHubProgressLog("owner/repo", "run-123", cli=cli)
    progress.append("## Iter 1\n\nA completed turn.\n\n")
    assert progress.read() == "# Experiment Progress\n\n## Iter 1\n\nA completed turn.\n\n"

    tracker = GitHubIssueTracker("owner/repo", cli=cli)
    assert tracker.list() == []
