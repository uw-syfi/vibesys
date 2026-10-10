"""Scaffolding for the ``GitRepository`` contract suite.

The suite (``libs/vs-project/tests/git_contract``) drives an implementation
only through its interface and the working-directory filesystem, so a Fake that
keeps history in memory passes the same cases as the Git CLI. ``ContractProject`` is the
one place that knows how to build the states the cases need.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from vs_project.api import GitRepository

type RepositoryFactory = Callable[[Path], GitRepository]
"""Build an implementation serving the repository whose worktree top is the path."""


def twin(
    sandbox: ContractProject,
    oracle_factory: RepositoryFactory,
    populate: Callable[[ContractProject], None],
) -> ContractProject:
    """A second project directory served by the oracle and built by the same ``populate``.

    Comparison cases ask the implementation and the oracle the same question about two
    identical directories, never about one, so they hold for an implementation whose
    repository is not on disk.
    """
    root = sandbox.root.parent / "oracle"
    root.mkdir()
    other = ContractProject(root=root, factory=oracle_factory)
    populate(other)
    return other


@dataclass
class ContractProject:
    """One project directory served by the implementation under test."""

    root: Path
    factory: RepositoryFactory

    def __post_init__(self) -> None:
        """Open the repository the factory serves at ``root``."""
        self.repo = self.factory(self.root)

    def reopen(self) -> GitRepository:
        """A new instance over the same directory, as after a process restart."""
        return self.factory(self.root)

    def start(self, files: Mapping[str, str] | None = None) -> str:
        """Initialize and bind a repository with one commit holding ``files`` (relative paths)."""
        self.repo.initialize(initial_branch="main")
        self.repo.bind()
        for path, text in (files or {}).items():
            self.write(path, text)
        return self.commit_all("baseline", allow_empty=True)

    def write(self, relative: str, text: str) -> Path:
        """Write ``text`` to ``relative`` under the project, creating parent directories."""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def delete(self, relative: str) -> None:
        """Remove the file ``relative`` from the project directory."""
        (self.root / relative).unlink()

    def commit_all(self, message: str, *, allow_empty: bool = False) -> str:
        """Stage everything, commit, and return the new ``HEAD``."""
        self.repo.stage_all(["."])
        self.repo.commit(message, allow_empty=allow_empty)
        head = self.repo.head()
        if head is None:
            message = "the repository has no HEAD after committing"
            raise AssertionError(message)
        return head
