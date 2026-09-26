"""Public adapter for the GitHub CLI.

``GitHubCLI`` runs GitHub operations through ``gh``. Failures are reported as
``GitHubCLIError`` or its unavailable and authentication subclasses.
"""

from collections.abc import Sequence
from typing import Protocol

from vs_github.cli import (
    GitHubAuthenticationError,
    GitHubCLI,
    GitHubCLIError,
    GitHubCLIUnavailableError,
)


class GitHubClient(Protocol):
    """Issue operations required by GitHub-backed VibeSys libraries."""

    def ensure_authenticated(self) -> None:
        """Raise an authentication error when the active gh account is absent."""
        ...

    def ensure_label(self, repository: str, /, name: str, *, color: str = "ededed") -> None:
        """Create a repository label when it does not exist."""
        ...

    def list_issues(self, repository: str, /) -> list[dict[str, object]]:
        """Return issue snapshots for one repository."""
        ...

    def view_issue(self, repository: str, issue_number: int, /) -> dict[str, object]:
        """Return one issue snapshot by number."""
        ...

    def create_issue(
        self, repository: str, /, *, title: str, body: str, labels: Sequence[str]
    ) -> int:
        """Create an issue and return its number."""
        ...

    def edit_issue(
        self,
        repository: str,
        issue_number: int,
        /,
        *,
        title: str | None = None,
        add_labels: Sequence[str] = (),
        remove_labels: Sequence[str] = (),
    ) -> None:
        """Update an issue title or labels."""
        ...

    def comment_issue(self, repository: str, issue_number: int, body: str, /) -> None:
        """Append a comment to an issue."""
        ...

    def set_issue_state(self, repository: str, issue_number: int, /, *, issue_open: bool) -> None:
        """Open or close an issue."""
        ...


__all__ = [
    "GitHubAuthenticationError",
    "GitHubCLI",
    "GitHubCLIError",
    "GitHubCLIUnavailableError",
    "GitHubClient",
]
