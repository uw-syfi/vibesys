"""Remote publication for a canonical VibeSys project repository."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable  # noqa: TC003  # tracked: #288
from dataclasses import dataclass, field
from pathlib import Path

from vibesys.repository import REPOSITORY_SLUG, RepositoryVisibility
from vs_github.api import GitHubCLI

_RUN_BRANCH_PREFIXES = ("vibesys-runs/", "vibesys/")
_GITHUB_ORIGIN = re.compile(
    r"^(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class ExperimentRepository:
    """Attach and publish the already-authored branch for one project run.

    This boundary never stages files or creates commits. ``GitTracker`` is the
    sole owner of project history.
    """

    root: Path
    log: Callable[[str], None]
    github: GitHubCLI = field(default_factory=GitHubCLI)

    def create_remote(self, slug: str, visibility: RepositoryVisibility) -> None:
        """Create a GitHub repository and attach it as ``origin``."""
        self._require_project_root()
        if not REPOSITORY_SLUG.fullmatch(slug):
            raise ValueError(f"--repo must be a GitHub OWNER/NAME pair, got {slug!r}")  # noqa: TRY003  # tracked: #288
        if self.has_origin():
            raise ValueError(f"project repository already has an origin remote: {self.root}")  # noqa: TRY003  # tracked: #288

        self.github.create_repository(
            slug,
            visibility=visibility.value,
            source=self.root,
        )
        self.log(f"[repo] created GitHub repository {slug}")

    def attach_remote(self, url: str) -> None:
        """Attach an existing remote repository as ``origin``."""
        self._require_project_root()
        if not url.strip():
            raise ValueError("origin URL must not be empty")  # noqa: TRY003  # tracked: #288
        if self.has_origin():
            raise ValueError(f"project repository already has an origin remote: {self.root}")  # noqa: TRY003  # tracked: #288
        self._run(["git", "remote", "add", "origin", url], tool="git")
        self.log("[repo] attached origin remote")

    def has_origin(self) -> bool:
        """Return whether the project repository has an ``origin`` remote."""
        result = self._run(
            ["git", "remote", "get-url", "origin"],
            check=False,
            tool="git",
        )
        return result.returncode == 0

    def origin_matches(self, repository: str) -> bool:
        """Return whether ``origin`` addresses the requested GitHub slug."""
        if not REPOSITORY_SLUG.fullmatch(repository):
            return False
        result = self._run(
            ["git", "remote", "get-url", "origin"],
            check=False,
            tool="git",
        )
        if result.returncode != 0:
            return False
        match = _GITHUB_ORIGIN.fullmatch(result.stdout.strip())
        return match is not None and match.group("slug") == repository

    def current_run_branch_tracks_origin(self) -> bool:
        """Return whether the current run branch already tracks ``origin``."""
        try:
            branch = self._current_run_branch()
        except ValueError:
            return False
        result = self._run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            check=False,
            tool="git",
        )
        return result.returncode == 0 and result.stdout.strip() == f"origin/{branch}"

    def push(self) -> None:
        """Push the already-committed current VibeSys run branch."""
        if not self.has_origin():
            return
        self._require_project_root()
        branch = self._current_run_branch()
        ref = f"refs/heads/{branch}"
        run_id = next(
            branch.removeprefix(prefix)
            for prefix in _RUN_BRANCH_PREFIXES
            if branch.startswith(prefix)
        )
        candidate_prefix = f"refs/vibesys/{run_id}/candidates/"
        candidate_refs = self._run(
            ["git", "for-each-ref", "--format=%(refname)", candidate_prefix],
            tool="git",
        ).stdout.splitlines()
        refspecs = [f"{ref}:{ref}", *(f"{candidate}:{candidate}" for candidate in candidate_refs)]
        self._run(["git", "push", "-u", "origin", *refspecs], tool="git")
        self.log(f"[repo] pushed {branch} to origin")

    def sync(self) -> None:
        """Compatibility name for :meth:`push`, without staging or committing."""
        self.push()

    def _current_run_branch(self) -> str:
        result = self._run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            check=False,
            tool="git",
        )
        branch = result.stdout.strip() if result.returncode == 0 else ""
        if not any(
            branch.startswith(prefix) and branch.removeprefix(prefix)
            for prefix in _RUN_BRANCH_PREFIXES
        ):
            raise ValueError(  # noqa: TRY003  # tracked: #288
                "remote publication requires the current VibeSys run branch"
            )
        return branch

    def _require_project_root(self) -> None:
        result = self._run(
            ["git", "rev-parse", "--show-toplevel"],
            check=False,
            tool="git",
        )
        if result.returncode != 0:
            raise ValueError(f"project directory is not a Git repository: {self.root}")  # noqa: TRY003  # tracked: #288
        repository_root = Path(result.stdout.strip()).resolve()
        if repository_root != self.root.resolve():
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"project directory must be the Git repository root: {self.root}"
            )

    def _run(
        self,
        command: list[str],
        *,
        check: bool = True,
        tool: str,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        for variable in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(variable, None)
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(self.root.resolve()),
            }
        )
        try:
            result = subprocess.run(  # noqa: PLW1510, S603  # tracked: #288
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"{tool} is required for project repository publication") from exc  # noqa: TRY003  # tracked: #288
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"{tool} command failed ({' '.join(command)}): {detail}")  # noqa: TRY003  # tracked: #288
        return result
