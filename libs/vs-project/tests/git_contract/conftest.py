"""Registry of ``GitRepository`` implementations the contract suite runs against.

Adding an implementation is one more ``"name": factory`` entry in ``IMPLEMENTATIONS``: a factory taking
the directory that is the worktree top level and returning the implementation
serving it. Every case in this directory runs against every entry, with no
per-implementation skips; an implementation that cannot pass a case narrows the
interface instead (see the software-design skill, rule 3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_project.api import CliGitRepository, NullGitTrackerEvents, Pygit2GitRepository
from vs_project.api.testing import ContractProject, FakeGitRepositories

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import GitRepository
    from vs_project.api.testing import RepositoryFactory


def _cli(root: Path) -> GitRepository:
    return CliGitRepository(root, faults=NullGitTrackerEvents())


def _pygit2(root: Path) -> GitRepository:
    return Pygit2GitRepository(root, faults=NullGitTrackerEvents())


IMPLEMENTATIONS: dict[str, RepositoryFactory] = {
    "cli": _cli,
    "pygit2": _pygit2,
    "fake": FakeGitRepositories(),
}

_ORACLE: RepositoryFactory = _cli
"""The reference the property-based cases compare every implementation to."""

ON_DISK: frozenset[str] = frozenset({"cli", "pygit2"})
"""Implementations whose repository is a real ``.git`` on disk, which plain ``git`` can share.

A module that sets ``REQUIRES_ON_DISK_REPOSITORY = True`` tests that capability (an agent
running ``git`` in the tracked workspace, lock files) and runs only against these. Every
other module runs against every implementation.
"""


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Run each case against every implementation that has what its module needs."""
    if "factory" not in metafunc.fixturenames:
        return
    needs_disk = getattr(metafunc.module, "REQUIRES_ON_DISK_REPOSITORY", False)
    names = [name for name in IMPLEMENTATIONS if not needs_disk or name in ON_DISK]
    metafunc.parametrize(
        "factory", [IMPLEMENTATIONS[name] for name in names], ids=names, scope="session"
    )


@pytest.fixture(scope="session")
def oracle_factory() -> RepositoryFactory:
    return _ORACLE


@pytest.fixture
def sandbox(factory: RepositoryFactory, tmp_path: Path) -> ContractProject:
    root = tmp_path / "project"
    root.mkdir()
    return ContractProject(root=root, factory=factory)
