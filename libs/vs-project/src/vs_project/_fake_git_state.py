"""The durable state behind ``FakeGitRepository``: repositories, checkouts, and the host that holds them.

``FakeGitRepositories`` plays the part of the disk: it survives a "restart" (a new
``FakeGitRepository`` over the same directory sees every earlier commit, ref, and index
entry) and it is the one place that knows which directories are worktrees of which
repository. Working-tree *files* are not kept here; they are the real directory.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vs_project._git_objects import EMPTY_SNAPSHOT, FileEntry, ObjectStore, Snapshot

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api.git_repository import GitError


@dataclass(frozen=True)
class RepositoryCensus:
    """How much a repository holds: what a test counts to see that an effect happened."""

    worktrees: int
    commits: int
    """Commits reachable from any branch, tag, retained ref, or worktree ``HEAD``."""
    refs: int


@dataclass
class Head:
    """``HEAD`` of one checkout: a branch name, or a detached commit."""

    branch: str | None
    """The branch ``HEAD`` names; ``None`` when detached."""
    detached: str | None = None


@dataclass(eq=False)
class Checkout:
    """One worktree of a repository: its directory, ``HEAD``, and index."""

    path: Path
    repo: Repository
    head: Head
    name: str | None = None
    """The administrative name of a linked worktree; ``None`` for the main one."""
    index: dict[str, FileEntry] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)
    """Commits made through this checkout (its reflog), until it is removed."""


@dataclass(eq=False)
class Repository:
    """Objects, refs, and checkouts shared by a main worktree and its linked ones."""

    git_dir: Path
    objects: ObjectStore = field(default_factory=ObjectStore)
    refs: dict[str, str] = field(default_factory=dict)
    checkouts: list[Checkout] = field(default_factory=list)

    @property
    def main(self) -> Checkout:
        """The main worktree."""
        return self.checkouts[0]

    @property
    def exclude_file(self) -> Path:
        """The repository-local ignore rules, shared by every worktree."""
        return self.git_dir / "info" / "exclude"

    def head_commit(self, checkout: Checkout) -> str | None:
        """The commit ``checkout``'s ``HEAD`` resolves to; ``None`` when unborn."""
        if checkout.head.branch is not None:
            return self.refs.get(f"refs/heads/{checkout.head.branch}")
        return checkout.head.detached

    def head_snapshot(self, checkout: Checkout) -> Snapshot:
        """The files of ``HEAD``'s tree (empty when unborn)."""
        commit = self.head_commit(checkout)
        if commit is None:
            return EMPTY_SNAPSHOT
        return self.objects.snapshot_of(commit)

    def advance_head(self, checkout: Checkout, commit: str) -> None:
        """Move ``checkout``'s branch (or detached ``HEAD``) to ``commit``."""
        if checkout.head.branch is not None:
            self.refs[f"refs/heads/{checkout.head.branch}"] = commit
        else:
            checkout.head.detached = commit

    def resolve(self, checkout: Checkout, revision: str) -> str | None:
        """The commit a ``HEAD``, ref name, or (abbreviated) commit id names."""
        if revision == "HEAD":
            return self.head_commit(checkout)
        if self.objects.commit(revision) is not None:
            return revision
        for ref in (
            revision,
            f"refs/{revision}",
            f"refs/tags/{revision}",
            f"refs/heads/{revision}",
        ):
            if ref in self.refs:
                return self.refs[ref]
        matches = self.objects.commits_named(revision)
        return matches[0] if len(matches) == 1 else None

    def conflicts_with_existing_ref(self, ref: str) -> bool:
        """Whether ``ref`` would be a file where a directory of refs exists, or the reverse."""
        if any(existing.startswith(f"{ref}/") for existing in self.refs):
            return True
        parts = ref.split("/")
        return any("/".join(parts[:count]) in self.refs for count in range(1, len(parts)))


class GitDisk:
    """All the repositories on one in-memory "disk", found by directory."""

    def __init__(self) -> None:
        """Start with no repositories."""
        self.lock = threading.RLock()
        self._checkouts: dict[Path, Checkout] = {}
        self._failures: dict[str, list[GitError]] = {}

    # -- lookup -------------------------------------------------------------------

    def locate(self, directory: Path) -> Checkout | None:
        """The checkout whose directory is ``directory`` or the nearest one containing it."""
        resolved = directory.resolve()
        for candidate in (resolved, *resolved.parents):
            found = self._checkouts.get(candidate)
            if found is not None:
                return found
        return None

    def register(self, checkout: Checkout) -> None:
        """Make ``checkout`` findable by its directory."""
        checkout.repo.checkouts.append(checkout)
        self._checkouts[checkout.path] = checkout

    def unregister(self, checkout: Checkout) -> None:
        """Forget a linked worktree and its reflog."""
        checkout.repo.checkouts.remove(checkout)
        del self._checkouts[checkout.path]

    def exact(self, directory: Path) -> Checkout | None:
        """The checkout whose directory is exactly ``directory``."""
        return self._checkouts.get(directory.resolve())

    def worktrees(self, directory: Path) -> tuple[Path, ...]:
        """Directories of every worktree of the repository containing ``directory``, main first."""
        found = self.locate(directory)
        return () if found is None else tuple(item.path for item in found.repo.checkouts)

    def census(self, directory: Path) -> RepositoryCensus:
        """What the repository containing ``directory`` holds now."""
        found = self.locate(directory)
        if found is None:
            return RepositoryCensus(0, 0, 0)
        repo = found.repo
        tips = set(repo.refs.values())
        tips.update(tip for tip in map(repo.head_commit, repo.checkouts) if tip is not None)
        reachable = {record.id for tip in tips for record in repo.objects.ancestry(tip)}
        return RepositoryCensus(len(repo.checkouts), len(reachable), len(repo.refs))

    def stale(self) -> list[Checkout]:
        """Linked worktrees whose directory no longer exists."""
        return [
            checkout
            for checkout in self._checkouts.values()
            if checkout.name is not None and not checkout.path.exists()
        ]

    # -- fault injection ----------------------------------------------------------

    def fail_next(self, operation: str, error: GitError) -> None:
        """Make the next call of the ``GitRepository`` method ``operation`` raise ``error``.

        The failure is raised before the call changes anything, like a refused command.
        Failures queue: each call of that method consumes one.
        """
        self._failures.setdefault(operation, []).append(error)

    def take_failure(self, operation: str) -> GitError | None:
        """Consume the oldest queued failure for ``operation``."""
        queue = self._failures.get(operation)
        return queue.pop(0) if queue else None
