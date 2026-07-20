"""Typed wrapper around the authenticated GitHub CLI."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
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
            raise GitHubCLIUnavailableError(
                "GitHub CLI (`gh`) is required for remote experiment repositories. "
                "Install it from https://cli.github.com/ and retry."
            ) from exc
        if check and result.returncode != 0:
            detail = _command_detail(result) or "unknown error"
            raise GitHubCLIError(f"GitHub CLI command failed ({' '.join(command)}): {detail}")
        return result


def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or "").strip() or (result.stdout or "").strip()
