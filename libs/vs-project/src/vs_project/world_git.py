"""The Git a test world runs on: the in-memory Fake by default, the product default for end-to-end cases.

Most composition tests need Git only as a means (a repository to commit to, worktrees to
branch candidates from), so their worlds run on ``FakeGitRepositories`` and spawn no
process. A world opened with ``GitKind.REAL`` runs the production implementation; keep
a few of those so the real path stays covered end to end.

``WorldGit`` is what the scripted *agent* of a world needs: it edits a candidate worktree
and commits there, which in production is the agent running ``git``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol

from vs_project._fake_git_repository import FakeGitRepositories
from vs_project._git_process import run_git

IN_MEMORY_GIT = FakeGitRepositories()
"""One in-memory disk for harnesses that build a fresh run per call.

A repository is found by its directory, and every test works under its own temporary
directory, so sharing the disk across tests shares nothing. A run that resumes finds the
repository its predecessor left, which is why the disk outlives one run.
"""


class WorldGitError(RuntimeError):
    """The world's Git could not do what the scripted agent or an assertion asked."""


class GitKind(StrEnum):
    """Which Git implementation a world's runtime resources use."""

    FAKE = "fake"
    """``FakeGitRepositories``: nothing is spawned and no ``.git`` exists on disk."""

    REAL = "real"
    """The product default (``open_git_repository``): a real repository on disk."""


class WorldGit(Protocol):
    """Git as the scripted agent and the assertions of a world see it."""

    def worktrees(self) -> list[Path]:
        """Directories of every worktree of the project repository, main checkout first."""
        ...

    def commit_all(self, tree: Path, message: str) -> str:
        """Stage everything in the worktree ``tree``, commit, and return the new commit."""
        ...

    def tree_of(self, commit: str) -> str:
        """The id of the tree ``commit`` records (equal trees hold equal files)."""
        ...

    def effects(self) -> int:
        """Worktrees, commits, and refs the repository holds, summed: a count that grows with work."""
        ...


class CliWorldGit:
    """Real Git, run as an agent in a sandbox would."""

    def __init__(self, root: Path) -> None:
        """Serve the project repository at ``root``."""
        self._root = root

    def _git(self, cwd: Path, *args: str) -> str:
        identity = ["-c", "user.name=agent", "-c", "user.email=agent@example.com"]
        result = run_git([*identity, *args], cwd=cwd)
        if result.returncode != 0:
            raise WorldGitError(result.stderr.decode())
        return result.stdout.decode()

    def worktrees(self) -> list[Path]:
        """See :class:`WorldGit`."""
        listing = self._git(self._root, "worktree", "list", "--porcelain")
        return [
            Path(line.removeprefix("worktree "))
            for line in listing.splitlines()
            if line.startswith("worktree ")
        ]

    def commit_all(self, tree: Path, message: str) -> str:
        """See :class:`WorldGit`."""
        self._git(tree, "add", "-A")
        self._git(tree, "commit", "-m", message)
        return self._git(tree, "rev-parse", "HEAD").strip()

    def tree_of(self, commit: str) -> str:
        """See :class:`WorldGit`."""
        return self._git(self._root, "rev-parse", f"{commit}^{{tree}}").strip()

    def effects(self) -> int:
        """See :class:`WorldGit`."""
        worktrees = len(self._git(self._root, "worktree", "list").splitlines())
        commits = int(self._git(self._root, "rev-list", "--all", "--count"))
        refs = len(self._git(self._root, "for-each-ref", "--count=1000").splitlines())
        return worktrees + commits + refs


class FakeWorldGit:
    """The same operations on the in-memory disk."""

    def __init__(self, root: Path, disk: FakeGitRepositories) -> None:
        """Serve the project repository at ``root`` on ``disk``."""
        self._root = root
        self._disk = disk

    def worktrees(self) -> list[Path]:
        """See :class:`WorldGit`."""
        return list(self._disk.worktrees(self._root))

    def commit_all(self, tree: Path, message: str) -> str:
        """See :class:`WorldGit`."""
        repository = self._disk.repository(tree)
        repository.stage_all(["."])
        repository.commit(message)
        head = repository.head()
        if head is None:
            message = f"{tree} has no commit after committing"
            raise WorldGitError(message)
        return head

    def effects(self) -> int:
        """See :class:`WorldGit`."""
        census = self._disk.census(self._root)
        return census.worktrees + census.commits + census.refs

    def tree_of(self, commit: str) -> str:
        """See :class:`WorldGit`."""
        tree = self._disk.repository(self._root).tree_of(commit)
        if tree is None:
            message = f"unknown commit {commit}"
            raise WorldGitError(message)
        return tree


def world_git(root: Path, disk: FakeGitRepositories | None) -> WorldGit:
    """The ``WorldGit`` for a world whose Fake disk is ``disk`` (``None`` for real Git)."""
    return CliWorldGit(root) if disk is None else FakeWorldGit(root, disk)
