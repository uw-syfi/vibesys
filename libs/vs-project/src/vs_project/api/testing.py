"""The owned in-memory test implementations for ``vs_project.api``.

``FakeGitRepositories`` is the Fake of ``GitRepository``: history, refs, and the index
live in memory, the working tree is the real directory. It passes the same contract
suite as ``CliGitRepository`` (``libs/vs-project/tests/git_contract``).
"""

from vs_project._fake_git_repository import FakeGitRepositories, FakeGitRepository
from vs_project._fake_git_state import RepositoryCensus

__all__ = ["FakeGitRepositories", "FakeGitRepository", "RepositoryCensus"]
