"""Remote publication for a canonical VibeSys project repository."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibesys.repository import REPOSITORY_SLUG, RepositoryVisibility
from vs_github.api import GitHubCLI
from vs_project.api import GitRemoteRepository

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
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

    @property
    def _repository(self) -> GitRemoteRepository:
        return GitRemoteRepository(self.root)

    def create_remote(self, slug: str, visibility: RepositoryVisibility) -> None:
        """Create a GitHub repository and attach it as ``origin``."""
        self._repository.require_root()
        if not REPOSITORY_SLUG.fullmatch(slug):
            message = f"--repo must be a GitHub OWNER/NAME pair, got {slug!r}"
            raise ValueError(message)
        if self.has_origin():
            _exception_message = f"project repository already has an origin remote: {self.root}"
            raise ValueError(_exception_message)

        self.github.create_repository(
            slug,
            visibility=visibility.value,
            source=self.root,
        )
        self.log(f"[repo] created GitHub repository {slug}")

    def attach_remote(self, url: str) -> None:
        """Attach an existing remote repository as ``origin``."""
        self._repository.attach_origin(url)
        self.log("[repo] attached origin remote")

    def has_origin(self) -> bool:
        """Return whether the project repository has an ``origin`` remote."""
        return self._repository.has_origin()

    def origin_matches(self, repository: str) -> bool:
        """Return whether ``origin`` addresses the requested GitHub slug."""
        if not REPOSITORY_SLUG.fullmatch(repository):
            return False
        origin = self._repository.origin_url()
        if origin is None:
            return False
        match = _GITHUB_ORIGIN.fullmatch(origin)
        return match is not None and match.group("slug") == repository

    def current_run_branch_tracks_origin(self) -> bool:
        """Return whether the current run branch already tracks ``origin``."""
        try:
            branch = self._current_run_branch()
        except ValueError:
            return False
        return self._repository.upstream() == f"origin/{branch}"

    def push(self) -> None:
        """Push the already-committed current VibeSys run branch."""
        if not self.has_origin():
            return
        self._repository.require_root()
        branch = self._current_run_branch()
        ref = f"refs/heads/{branch}"
        run_id = next(
            branch.removeprefix(prefix)
            for prefix in _RUN_BRANCH_PREFIXES
            if branch.startswith(prefix)
        )
        candidate_prefix = f"refs/vibesys/{run_id}/candidates/"
        candidate_refs = self._repository.refs(candidate_prefix)
        refspecs = [f"{ref}:{ref}", *(f"{candidate}:{candidate}" for candidate in candidate_refs)]
        self._repository.push_origin(refspecs)
        self.log(f"[repo] pushed {branch} to origin")

    def _current_run_branch(self) -> str:
        branch = self._repository.current_branch() or ""
        if not any(
            branch.startswith(prefix) and branch.removeprefix(prefix)
            for prefix in _RUN_BRANCH_PREFIXES
        ):
            message = "remote publication requires the current VibeSys run branch"
            raise ValueError(message)
        return branch
