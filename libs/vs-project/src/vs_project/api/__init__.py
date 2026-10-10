"""Public project layout, run metadata, and typed state persistence.

``Project`` is the entry point for repository state. The exported records and
errors describe its configuration, run state, Git integration, and task paths.
``strip_ansi`` is also public for consumers of ``RunLogger`` output.

Runs use one version 5 manifest containing an ``OrchestrationDescriptor``.
"""

from typing import TYPE_CHECKING

from vs_project._cli_git_repository import CliGitRepository
from vs_project._git_backend import (
    DEFAULT_GIT_BACKEND,
    GIT_BACKEND_ENV,
    GitBackend,
    GitBackendError,
    open_git_repository,
    pygit2_installed,
    select_git_backend,
)
from vs_project._git_events import GitTrackerEvents, NullGitTrackerEvents
from vs_project._git_process import run_git
from vs_project._git_remote import GitRemoteRepository
from vs_project._git_tracker import FrameworkSnapshotStatus, GitTracker
from vs_project._layout import (
    AmbiguousTaskError,
    ConfigurationRoot,
    InvalidTaskDefinitionError,
    InvalidTaskNameError,
    ProjectLayoutError,
    ProjectNotInitializedError,
    ProjectRootNotFoundError,
    TaskDirectory,
    TaskName,
    TaskNotFoundError,
    TasksRoot,
    UnsafeProjectPathError,
)
from vs_project._logger import RunLogger, strip_ansi
from vs_project._manifests import (
    AgentRoleExecutionRecord,
    GitObjectId,
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    ProjectManifest,
    RunEnvironmentRecord,
    RunExecutionRecord,
    RunResourceRequest,
)
from vs_project._socket import (
    MAX_SOCKET_PATH_BYTES,
    SocketPathTooLongError,
    validate_socket_path,
)
from vs_project._state import (
    PROJECT_SCHEMA_VERSION,
    RUN_SCHEMA_VERSION,
    GitSnapshotFile,
    GitSnapshotPlan,
    ProjectGitIntegration,
    ProjectSandboxPaths,
    ProjectStateError,
    StateFile,
    StateModelNotFoundError,
    StateNamespace,
    StateSlot,
    StateSnapshot,
    StateTransition,
    generate_run_id,
    is_project_state_path,
    validate_run_id,
)
from vs_project._state_io import (
    AtomicWriteEffects,
    AtomicWriteStream,
    LocalAtomicWriteEffects,
    atomic_write_bytes,
    decode_state_document,
)
from vs_project._state_models import FakeStateModels, validate_state_namespace
from vs_project._state_store import FakeStateStore, FakeStateStores, LocalStateStore
from vs_project.api.git_repository import (
    COMMIT_IDENTITY_EMAIL,
    COMMIT_IDENTITY_NAME,
    CommitSubject,
    GitCommandError,
    GitError,
    GitFaultSink,
    GitRepository,
    GitRepositoryFactory,
    GitTimeoutError,
    PatchStyle,
    Pathspec,
    RepositoryLocation,
    Revision,
    StagingError,
)
from vs_project.api.state_models import StateModels
from vs_project.api.state_store import (
    CommitFault,
    CommitOutcome,
    Committed,
    Conflict,
    ConflictReason,
    ObservationFault,
    QuarantinedEnvelope,
    StateStore,
    StateStoreFactory,
    StateStoreWriteError,
    StoredEnvelope,
    StoreFence,
    StoreRecord,
    Unknown,
)
from vs_project.errors import ProjectError, StateDocumentDamagedError
from vs_project.project import Project

if TYPE_CHECKING:
    from vs_project._pygit2_git_repository import Pygit2GitRepository

__all__ = [
    "COMMIT_IDENTITY_EMAIL",
    "COMMIT_IDENTITY_NAME",
    "DEFAULT_GIT_BACKEND",
    "GIT_BACKEND_ENV",
    "MAX_SOCKET_PATH_BYTES",
    "PROJECT_SCHEMA_VERSION",
    "RUN_SCHEMA_VERSION",
    "AgentRoleExecutionRecord",
    "AmbiguousTaskError",
    "AtomicWriteEffects",
    "AtomicWriteStream",
    "CliGitRepository",
    "CommitFault",
    "CommitOutcome",
    "CommitSubject",
    "Committed",
    "ConfigurationRoot",
    "Conflict",
    "ConflictReason",
    "FakeStateModels",
    "FakeStateStore",
    "FakeStateStores",
    "FrameworkSnapshotStatus",
    "GitBackend",
    "GitBackendError",
    "GitCommandError",
    "GitError",
    "GitFaultSink",
    "GitObjectId",
    "GitRemoteRepository",
    "GitRepository",
    "GitRepositoryFactory",
    "GitSnapshotFile",
    "GitSnapshotPlan",
    "GitTimeoutError",
    "GitTracker",
    "GitTrackerEvents",
    "InvalidTaskDefinitionError",
    "InvalidTaskNameError",
    "LocalAtomicWriteEffects",
    "LocalStateStore",
    "NullGitTrackerEvents",
    "ObservationFault",
    "OrchestrationDescriptor",
    "OrchestrationRunManifest",
    "PatchStyle",
    "Pathspec",
    "Project",
    "ProjectError",
    "ProjectGitIntegration",
    "ProjectLayoutError",
    "ProjectManifest",
    "ProjectNotInitializedError",
    "ProjectRootNotFoundError",
    "ProjectSandboxPaths",
    "ProjectStateError",
    "Pygit2GitRepository",
    "QuarantinedEnvelope",
    "RepositoryLocation",
    "Revision",
    "RunEnvironmentRecord",
    "RunExecutionRecord",
    "RunLogger",
    "RunResourceRequest",
    "SocketPathTooLongError",
    "StagingError",
    "StateDocumentDamagedError",
    "StateFile",
    "StateModelNotFoundError",
    "StateModels",
    "StateNamespace",
    "StateSlot",
    "StateSnapshot",
    "StateStore",
    "StateStoreFactory",
    "StateStoreWriteError",
    "StateTransition",
    "StoreFence",
    "StoreRecord",
    "StoredEnvelope",
    "TaskDirectory",
    "TaskName",
    "TaskNotFoundError",
    "TasksRoot",
    "Unknown",
    "UnsafeProjectPathError",
    "atomic_write_bytes",
    "decode_state_document",
    "generate_run_id",
    "is_project_state_path",
    "open_git_repository",
    "pygit2_installed",
    "run_git",
    "select_git_backend",
    "strip_ansi",
    "validate_run_id",
    "validate_socket_path",
    "validate_state_namespace",
]


def __getattr__(name: str) -> object:
    """Load ``Pygit2GitRepository`` on first use, so importing this package never needs ``pygit2``."""
    if name == "Pygit2GitRepository":
        from vs_project._pygit2_git_repository import (  # noqa: PLC0415  # lint-waiver: LW-415571 [PLC0415]; the libgit2 implementation imports pygit2, which this package must not require just to be imported.
            Pygit2GitRepository,
        )

        return Pygit2GitRepository
    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)
