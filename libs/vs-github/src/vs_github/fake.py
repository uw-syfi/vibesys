"""In-memory `GitHubClient` for tests of code that talks to GitHub issues."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence


class FakeGitHubClient:
    """An in-memory GitHub repository behind the `GitHubClient` interface."""

    def __init__(self) -> None:
        """Open an empty repository."""
        self.issues: dict[int, dict[str, Any]] = {}
        self.next_number = 1
        self.labels: set[str] = set()

    def ensure_authenticated(self) -> None:
        """Always authenticated."""

    def ensure_label(self, _repository: str, name: str, *, color: str = "ededed") -> None:
        """Remember the label."""
        del color
        self.labels.add(name)

    def list_issues(self, _repository: str) -> list[dict[str, object]]:
        """Snapshots of every issue."""
        return [dict(issue) for issue in self.issues.values()]

    def view_issue(self, _repository: str, issue_number: int) -> dict[str, object]:
        """One issue snapshot by number."""
        return dict(self.issues[issue_number])

    def create_issue(
        self,
        _repository: str,
        *,
        title: str,
        body: str,
        labels: Sequence[str],
    ) -> int:
        """Create an issue and return its number."""
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
        """Change the title or labels."""
        issue = self.issues[issue_number]
        if title is not None:
            issue["title"] = title
        labels = {item["name"] for item in issue["labels"]}
        labels.difference_update(remove_labels)
        labels.update(add_labels)
        issue["labels"] = [{"name": label} for label in sorted(labels)]

    def comment_issue(self, _repository: str, issue_number: int, body: str) -> None:
        """Append a comment."""
        self.issues[issue_number]["comments"].append({"body": body})

    def set_issue_state(self, _repository: str, issue_number: int, *, issue_open: bool) -> None:
        """Open or close the issue."""
        self.issues[issue_number]["state"] = "OPEN" if issue_open else "CLOSED"


__all__ = ["FakeGitHubClient"]
