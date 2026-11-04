"""The owned in-memory test implementations for ``vs_project.api``.

``FakeControlInbox`` is the Fake of ``ControlInbox`` and passes the same contract suite as
``FileControlInbox`` (``libs/vs-project/tests/test_control_inbox_contract.py``).

``FakeGitRepositories`` is the Fake of ``GitRepository``: history, refs, and the index
live in memory, the working tree is the real directory. It passes the same contract
suite as ``CliGitRepository`` (``libs/vs-project/tests/git_contract``).
"""

from vs_project._control_inbox import FakeControlInbox
from vs_project._fake_admission import FakeAdmissionStore
from vs_project._fake_git_repository import FakeGitRepositories, FakeGitRepository
from vs_project._fake_git_state import RepositoryCensus
from vs_project._run_history import FakeRunHistory
from vs_project.git_contract_scaffold import ContractProject, RepositoryFactory, twin
from vs_project.run_execution_fixture import run_execution_record
from vs_project.state_fixture import (
    scratch_state_directory,
    seed_store_document,
    seed_stored_record,
    stored_record_bytes,
)
from vs_project.world_git import (
    IN_MEMORY_GIT,
    CliWorldGit,
    FakeWorldGit,
    GitKind,
    WorldGit,
    WorldGitError,
    world_git,
)

__all__ = [
    "IN_MEMORY_GIT",
    "CliWorldGit",
    "ContractProject",
    "FakeAdmissionStore",
    "FakeControlInbox",
    "FakeGitRepositories",
    "FakeGitRepository",
    "FakeRunHistory",
    "FakeWorldGit",
    "GitKind",
    "RepositoryCensus",
    "RepositoryFactory",
    "WorldGit",
    "WorldGitError",
    "run_execution_record",
    "scratch_state_directory",
    "seed_store_document",
    "seed_stored_record",
    "stored_record_bytes",
    "twin",
    "world_git",
]
