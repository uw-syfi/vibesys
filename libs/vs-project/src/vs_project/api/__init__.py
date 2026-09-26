"""Public project layout, run metadata, and typed state persistence.

``Project`` is the entry point for repository state. The exported records and
errors describe its configuration, run state, Git integration, and task paths.
``strip_ansi`` is also public for consumers of ``RunLogger`` output.

Runs use one version 4 manifest containing an ``OrchestrationDescriptor``.
"""

from vs_project._git_events import GitTrackerEvents, NullGitTrackerEvents
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
)
from vs_project.errors import ProjectError
from vs_project.project import Project

__all__ = [
    "MAX_SOCKET_PATH_BYTES",
    "PROJECT_SCHEMA_VERSION",
    "RUN_SCHEMA_VERSION",
    "AmbiguousTaskError",
    "ConfigurationRoot",
    "FrameworkSnapshotStatus",
    "GitObjectId",
    "GitSnapshotFile",
    "GitSnapshotPlan",
    "GitTracker",
    "GitTrackerEvents",
    "InvalidTaskDefinitionError",
    "InvalidTaskNameError",
    "NullGitTrackerEvents",
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
    "RunEnvironmentRecord",
    "RunExecutionRecord",
    "RunLogger",
    "RunResourceRequest",
    "SocketPathTooLongError",
    "StateFile",
    "StateModelNotFoundError",
    "StateNamespace",
    "StateSlot",
    "StateSnapshot",
    "StateTransition",
    "TaskDirectory",
    "TaskName",
    "TaskNotFoundError",
    "TasksRoot",
    "UnsafeProjectPathError",
    "generate_run_id",
    "is_project_state_path",
    "strip_ansi",
    "validate_socket_path",
]
