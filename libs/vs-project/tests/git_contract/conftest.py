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
from tests.support.git_contract import Sandbox

from vs_project.api import CliGitRepository, NullGitTrackerEvents, Pygit2GitRepository

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.git_contract import RepositoryFactory

    from vs_project.api import GitRepository


def _cli(root: Path) -> GitRepository:
    return CliGitRepository(root, faults=NullGitTrackerEvents())


def _pygit2(root: Path) -> GitRepository:
    return Pygit2GitRepository(root, faults=NullGitTrackerEvents())


IMPLEMENTATIONS: dict[str, RepositoryFactory] = {
    "cli": _cli,
    "pygit2": _pygit2,
}

_ORACLE: RepositoryFactory = _cli
"""The reference the property-based cases compare every implementation to."""


@pytest.fixture(scope="session", params=list(IMPLEMENTATIONS.values()), ids=list(IMPLEMENTATIONS))
def factory(request: pytest.FixtureRequest) -> RepositoryFactory:
    return request.param


@pytest.fixture(scope="session")
def oracle_factory() -> RepositoryFactory:
    return _ORACLE


@pytest.fixture
def sandbox(factory: RepositoryFactory, tmp_path: Path) -> Sandbox:
    root = tmp_path / "project"
    root.mkdir()
    return Sandbox(root=root, factory=factory)
