"""A trusted checkout of the input revision, for work that belongs to no run."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import GitRepository
    from vs_sim.api import BlockingRunner


@dataclass(frozen=True, slots=True)
class _Root:
    path: Path
    repository: GitRepository
    blocking: BlockingRunner

    async def has_revision(self, revision: str) -> bool:
        return await self.blocking.run(self.repository.resolve_commit, revision) is not None


class _Checkout:
    def __init__(self, path: Path, root: _Root) -> None:
        self._path = path
        self._root = root
        self._removed = False

    @property
    def path(self) -> Path:
        return self._path

    async def discard(self) -> None:
        if not self._removed:
            self._removed = True
            await self._root.blocking.run(self._root.repository.remove_worktree, self._path)


class RepositoryCheckouts:
    """Fresh linked worktrees of a revision below *scratch*, each removed when discarded.

    A comparison measures the engine of its own image, so the checkout supplies only what the
    task's commands read: the input revision's tree, with no build step run in it.
    """

    def __init__(
        self, root: Path, repository: GitRepository, scratch: Path, blocking: BlockingRunner
    ) -> None:
        """Check out revisions of *repository* (rooted at *root*) below *scratch*."""
        self._root = _Root(root, repository, blocking)
        self._scratch = scratch
        self._numbers = itertools.count()

    @property
    def root(self) -> _Root:
        """The repository the revisions live in."""
        return self._root

    async def create_measured_candidate(self, revision: str) -> _Checkout:
        """A new worktree of *revision*."""
        destination = self._scratch / f"checkout-{next(self._numbers)}"
        await self._root.blocking.run(self._root.repository.add_worktree, destination, revision)
        return _Checkout(destination, self._root)
