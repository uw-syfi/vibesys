"""Local Git remote operations for one project repository."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from vs_project._git_process import git_environment

GitRunner = Callable[..., subprocess.CompletedProcess[str]]


class GitRemoteRepository:
    """Inspect and update one repository's Git remotes without authoring history.

    Commands are pinned to ``root`` and ignore inherited worktree-selection
    variables. Mutating operations require ``root`` to be the repository's
    top-level directory.
    """

    def __init__(
        self,
        root: Path,
        *,
        runner: GitRunner = subprocess.run,
    ) -> None:
        self.root = root
        self._runner = runner

    def require_root(self) -> None:
        """Raise unless ``root`` is the repository's top-level directory."""
        result = self._run(["git", "rev-parse", "--show-toplevel"], check=False)
        if result.returncode != 0:
            message = f"project directory is not a Git repository: {self.root}"
            raise ValueError(message)
        repository_root = Path(result.stdout.strip()).resolve()
        if repository_root != self.root.resolve():
            message = f"project directory must be the Git repository root: {self.root}"
            raise ValueError(message)

    def has_origin(self) -> bool:
        """Return whether the repository has an ``origin`` remote."""
        return self.origin_url() is not None

    def origin_url(self) -> str | None:
        """Return the configured ``origin`` URL, or ``None`` when absent."""
        result = self._run(
            ["git", "remote", "get-url", "origin"],
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def attach_origin(self, url: str) -> None:
        """Attach ``url`` as ``origin`` after validating repository ownership."""
        self.require_root()
        if not url.strip():
            message = "origin URL must not be empty"
            raise ValueError(message)
        if self.has_origin():
            message = f"project repository already has an origin remote: {self.root}"
            raise ValueError(message)
        self._run(["git", "remote", "add", "origin", url])

    def current_branch(self) -> str | None:
        """Return the symbolic current branch, or ``None`` for detached HEAD."""
        result = self._run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            check=False,
        )
        branch = result.stdout.strip()
        return branch if result.returncode == 0 and branch else None

    def upstream(self) -> str | None:
        """Return the current branch's upstream ref, or ``None`` when absent."""
        result = self._run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            check=False,
        )
        upstream = result.stdout.strip()
        return upstream if result.returncode == 0 and upstream else None

    def refs(self, prefix: str) -> tuple[str, ...]:
        """Return full local ref names beneath ``prefix``."""
        result = self._run(["git", "for-each-ref", "--format=%(refname)", prefix])
        return tuple(result.stdout.splitlines())

    def push_origin(self, refspecs: Sequence[str]) -> None:
        """Push exact ``refspecs`` to ``origin`` and configure upstream."""
        self.require_root()
        self._run(["git", "push", "-u", "origin", *refspecs])

    def _run(
        self,
        command: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        env = git_environment(safe_directory=self.root.resolve())
        try:
            result = self._runner(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        except FileNotFoundError as exc:
            message = "git is required for project repository operations"
            raise RuntimeError(message) from exc
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            message = f"git command failed ({' '.join(command)}): {detail}"
            raise RuntimeError(message)
        return result


__all__ = ["GitRemoteRepository"]
