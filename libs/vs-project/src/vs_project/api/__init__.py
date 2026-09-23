"""Public project layout, run metadata, and typed state persistence.

``Project`` is the entry point for repository state. The exported records and
errors describe its configuration, run state, Git integration, and task paths.
``strip_ansi`` is also public for consumers of ``RunLogger`` output.

Deprecated for new code: ``RunConfiguration`` and its concrete types,
``RunManifest``, ``compare_resume_configurations``, and the agent round methods
on ``Project.state``. They remain supported for version 3 run compatibility.
Use ``OrchestrationRunManifest`` and ``OrchestrationDescriptor`` for new runs.
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
    AgentRunConfiguration,
    EvolveRunConfiguration,
    GitObjectId,
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    PlainRunConfiguration,
    ProjectManifest,
    RunConfiguration,
    RunEnvironmentRecord,
    RunManifest,
    RunManifestRecord,
    RunResourceRequest,
)
from vs_project._resume import ResumeConfigurationComparison, compare_resume_configurations
from vs_project._socket import (
    MAX_SOCKET_PATH_BYTES,
    SocketPathTooLongError,
    validate_socket_path,
)
from vs_project._state import (
    ORCHESTRATION_RUN_SCHEMA_VERSION,
    PROJECT_SCHEMA_VERSION,
    RUN_SCHEMA_VERSION,
    GitSnapshotFile,
    GitSnapshotPlan,
    ProjectGitIntegration,
    ProjectSandboxPaths,
    ProjectStateError,
    RunSchemaMigrationRequiredError,
    StateFile,
    StateModelNotFoundError,
    StateNamespace,
    StateSlot,
    StateSnapshot,
    StateTransition,
    generate_run_id,
    is_project_state_path,
    serialize_round,
)
from vs_project.errors import ProjectError
from vs_project.project import Project

__all__ = [
    "MAX_SOCKET_PATH_BYTES",
    "ORCHESTRATION_RUN_SCHEMA_VERSION",
    "PROJECT_SCHEMA_VERSION",
    "RUN_SCHEMA_VERSION",
    "AgentRunConfiguration",
    "AmbiguousTaskError",
    "ConfigurationRoot",
    "EvolveRunConfiguration",
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
    "PlainRunConfiguration",
    "Project",
    "ProjectError",
    "ProjectGitIntegration",
    "ProjectLayoutError",
    "ProjectManifest",
    "ProjectNotInitializedError",
    "ProjectRootNotFoundError",
    "ProjectSandboxPaths",
    "ProjectStateError",
    "ResumeConfigurationComparison",
    "RunConfiguration",
    "RunEnvironmentRecord",
    "RunLogger",
    "RunManifest",
    "RunManifestRecord",
    "RunResourceRequest",
    "RunSchemaMigrationRequiredError",
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
    "compare_resume_configurations",
    "generate_run_id",
    "is_project_state_path",
    "serialize_round",
    "strip_ansi",
    "validate_socket_path",
]
