"""Public project layout, run metadata, and typed state persistence.

``Project`` is the entry point for repository state. The exported records and
errors describe its configuration, run state, Git integration, and task paths.
``strip_ansi`` is also public for consumers of ``RunLogger`` output.

Runs use one version 5 manifest containing an ``OrchestrationDescriptor``.
"""

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
)
from vs_project._state_models import FakeStateModels, validate_state_namespace
from vs_project._state_store import FakeStateStore, LocalStateStore
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
    StateStoreWriteError,
    StoredEnvelope,
    StoreFence,
    StoreRecord,
    Unknown,
)
from vs_project.errors import ProjectError
from vs_project.project import Project

__all__ = [
    "MAX_SOCKET_PATH_BYTES",
    "PROJECT_SCHEMA_VERSION",
    "RUN_SCHEMA_VERSION",
    "AgentRoleExecutionRecord",
    "AmbiguousTaskError",
    "AtomicWriteEffects",
    "AtomicWriteStream",
    "CommitFault",
    "CommitOutcome",
    "Committed",
    "ConfigurationRoot",
    "Conflict",
    "ConflictReason",
    "FakeStateModels",
    "FakeStateStore",
    "FrameworkSnapshotStatus",
    "GitObjectId",
    "GitRemoteRepository",
    "GitSnapshotFile",
    "GitSnapshotPlan",
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
    "Project",
    "ProjectError",
    "ProjectGitIntegration",
    "ProjectLayoutError",
    "ProjectManifest",
    "ProjectNotInitializedError",
    "ProjectRootNotFoundError",
    "ProjectSandboxPaths",
    "ProjectStateError",
    "QuarantinedEnvelope",
    "RunEnvironmentRecord",
    "RunExecutionRecord",
    "RunLogger",
    "RunResourceRequest",
    "SocketPathTooLongError",
    "StateFile",
    "StateModelNotFoundError",
    "StateModels",
    "StateNamespace",
    "StateSlot",
    "StateSnapshot",
    "StateStore",
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
    "generate_run_id",
    "is_project_state_path",
    "run_git",
    "strip_ansi",
    "validate_run_id",
    "validate_socket_path",
    "validate_state_namespace",
]
