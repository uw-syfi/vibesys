"""Typed wrapper around the authenticated GitHub CLI."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess[str]]


class GitHubCLIError(RuntimeError):
    """Base error for failed GitHub CLI operations."""


class GitHubCLIUnavailableError(GitHubCLIError):
    """Raised when the ``gh`` executable cannot be found."""


class GitHubAuthenticationError(GitHubCLIError):
    """Raised when ``gh`` has no valid credentials for the configured host."""


@dataclass(frozen=True)
class GitHubCLI:
    """Run authenticated GitHub repository operations through ``gh``."""

    hostname: str = "github.com"
    _runner: Runner = field(default=subprocess.run, repr=False, compare=False)

    def ensure_authenticated(self) -> None:
        """Raise an actionable error unless ``gh`` is authenticated."""
        result = self._run(
            ["auth", "status", "--hostname", self.hostname],
            check=False,
        )
        if result.returncode == 0:
            return
        detail = _command_detail(result)
        message = (
            f"GitHub CLI is not authenticated for {self.hostname}. "
            f"Run `gh auth login --hostname {self.hostname}` and retry."
        )
        if detail:
            message += f" GitHub CLI reported: {detail}"
        raise GitHubAuthenticationError(message)

    def current_user(self) -> str:
        """Return the login for the authenticated GitHub account."""
        self.ensure_authenticated()
        result = self._run(["api", "user", "--jq", ".login"])
        login = result.stdout.strip()
        if not login:
            message = "GitHub CLI returned an empty authenticated user."
            raise GitHubCLIError(message)
        return login

    def create_repository(
        self,
        repository: str,
        *,
        visibility: str,
        source: Path,
        remote_name: str = "origin",
    ) -> None:
        """Create a repository from a local source and add its Git remote."""
        self.ensure_authenticated()
        self._run(
            [
                "repo",
                "create",
                repository,
                f"--{visibility}",
                "--source",
                str(source),
                "--remote",
                remote_name,
            ],
            cwd=source,
        )

    def clone_repository(self, repository: str, destination: Path) -> None:
        """Clone a GitHub repository into ``destination``."""
        self.ensure_authenticated()
        self._run(["repo", "clone", repository, str(destination)])

    def list_issues(self, repository: str) -> list[dict[str, object]]:
        """Return all issues in a repository, excluding pull requests."""
        result = self._run(
            [
                "issue",
                "list",
                "--repo",
                repository,
                "--state",
                "all",
                "--limit",
                "1000",
                "--json",
                "number,title,body,state,labels,createdAt,updatedAt,author,url",
            ]
        )
        value = _decode_json(result.stdout, "issue list")
        if not isinstance(value, list):
            raise GitHubCLIError("GitHub CLI returned a non-list issue response.")  # noqa: TRY003  # tracked: #288
        return [item for item in value if isinstance(item, dict)]

    def view_issue(self, repository: str, issue_number: int) -> dict[str, object]:
        """Return one issue and its comments as JSON-compatible values."""
        result = self._run(
            [
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repository,
                "--json",
                "number,title,body,state,labels,createdAt,updatedAt,author,url,comments",
            ]
        )
        value = _decode_json(result.stdout, "issue view")
        if not isinstance(value, dict):
            raise GitHubCLIError("GitHub CLI returned a non-object issue response.")  # noqa: TRY003  # tracked: #288
        return value

    def create_issue(self, repository: str, *, title: str, body: str, labels: Sequence[str]) -> int:
        """Create an issue and return its GitHub number."""
        args = ["issue", "create", "--repo", repository, "--title", title, "--body", body]
        for label in labels:
            args.extend(("--label", label))
        result = self._run(args)
        try:
            return int(result.stdout.strip().rsplit("/", 1)[-1])
        except ValueError as exc:
            raise GitHubCLIError("GitHub CLI did not return the created issue URL.") from exc  # noqa: TRY003  # tracked: #288

    def edit_issue(
        self,
        repository: str,
        issue_number: int,
        *,
        title: str | None = None,
        add_labels: Sequence[str] = (),
        remove_labels: Sequence[str] = (),
    ) -> None:
        """Edit an issue title or labels without replacing its user body."""
        args = ["issue", "edit", str(issue_number), "--repo", repository]
        if title is not None:
            args.extend(("--title", title))
        for label in add_labels:
            args.extend(("--add-label", label))
        for label in remove_labels:
            args.extend(("--remove-label", label))
        self._run(args)

    def comment_issue(self, repository: str, issue_number: int, body: str) -> None:
        """Add a comment without modifying existing issue content."""
        self._run(["issue", "comment", str(issue_number), "--repo", repository, "--body", body])

    def set_issue_state(self, repository: str, issue_number: int, *, issue_open: bool) -> None:
        """Close or reopen an issue."""
        action = "reopen" if issue_open else "close"
        self._run(["issue", action, str(issue_number), "--repo", repository])

    def ensure_label(self, repository: str, name: str, *, color: str = "ededed") -> None:
        """Create a metadata label when it does not already exist."""
        result = self._run(
            ["label", "create", name, "--repo", repository, "--color", color],
            check=False,
        )
        if result.returncode != 0:
            detail = _command_detail(result).lower()
            if "already exists" not in detail:
                raise GitHubCLIError(_command_detail(result) or "Could not create GitHub label.")

    def _run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = ["gh", *arguments]
        try:
            result = self._runner(command, cwd=cwd, capture_output=True, text=True)
        except FileNotFoundError as exc:
            message = (
                "GitHub CLI (`gh`) is required for remote experiment repositories. "
                "Install it from https://cli.github.com/ and retry."
            )
            raise GitHubCLIUnavailableError(message) from exc
        if check and result.returncode != 0:
            detail = _command_detail(result) or "unknown error"
            message = f"GitHub CLI command failed ({' '.join(command)}): {detail}"
            raise GitHubCLIError(message)
        return result


def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or "").strip() or (result.stdout or "").strip()


def _decode_json(raw: str, operation: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubCLIError(f"GitHub CLI returned invalid JSON for {operation}.") from exc  # noqa: TRY003  # tracked: #288
