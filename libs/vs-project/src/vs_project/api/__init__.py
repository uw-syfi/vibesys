"""Public project layout, run metadata, and typed state persistence.

``Project`` is the entry point for repository state. The exported records and
errors describe its configuration, run state, Git integration, and task paths.
``strip_ansi`` is public for consumers of ``RunLogger`` output.

Runs use one version 5 manifest containing an ``OrchestrationDescriptor``.
"""

from importlib import import_module
from typing import TYPE_CHECKING

from vs_project._cli_git_repository import CliGitRepository
from vs_project._control_inbox import (
    INBOX_FORMAT,
    ControlInbox,
    Ended,
    FileControlInbox,
    InboxClosedError,
    InboxEntry,
    RejectedEntry,
    RequestState,
    RequestStatus,
    ended_unapplied,
    validate_request_id,
)
from vs_project._decode_memo import DecodeMemo
from vs_project._git_backend import (
    DEFAULT_GIT_BACKEND,
    GIT_BACKEND_ENV,
    GitBackend,
    GitBackendError,
    git_repositories,
    open_git_repository,
    pygit2_installed,
    select_git_backend,
)
from vs_project._git_events import GitTrackerEvents, NullGitTrackerEvents
from vs_project._git_process import Git, GitText
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
    canonical_project_root,
    resume_directory,
)
from vs_project._logger import LPRINT_LOGGER, RunLogger, strip_ansi
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
from vs_project._operator_key import (
    CONTROL_HELLO_MAX_BYTES,
    OPERATOR_KEY_NAME,
    OperatorDialer,
    OperatorKey,
    OperatorKeyError,
    OperatorSocketOutsideRootError,
    create_operator_key,
    operator_key_path,
    read_operator_key,
)
from vs_project._private_directory import (
    PrivateDirectoryError,
    ensure_private_directory,
    user_runtime_root,
)
from vs_project._run_branches import adoption_branch, run_branch
from vs_project._run_history import GitRunHistory
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
    StoreDurabilityEffects,
    atomic_write_bytes,
    decode_state_document,
)
from vs_project._state_models import FakeStateModels, validate_state_namespace
from vs_project._state_store import FakeStateStore, FakeStateStores, LocalStateStore
from vs_project._store_operations import StoreFile
from vs_project._task_files import (
    DEFAULT_READY_TIMEOUT_SECONDS,
    OBJECTIVES_NAME,
    PORT_PLACEHOLDER,
    AcceleratorVendor,
    CatalogueInput,
    ComparisonInput,
    ComparisonName,
    ExampleDomain,
    HardwareInput,
    ModelMaker,
    ObjectiveAxisInput,
    ObjectivesInput,
    ParetoInput,
    ServedModelInput,
    WorkloadInput,
    check_comparisons_are_distinct,
    comparison_directory,
    comparison_namespace,
    load_objectives,
)
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
    NestedRepositoryError,
    PatchStyle,
    Pathspec,
    RepositoryLocation,
    Revision,
    StagingError,
)
from vs_project.api.run_history import RecordedRuns, RunHistory
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
    from vs_project._admission import FileAdmissionStore
    from vs_project._pygit2_git_repository import Pygit2GitRepository
    from vs_project._revision_publication import (
        CheckoutOutcome,
        PublicationError,
        PublishedRevision,
    )
    from vs_project.api.admissions import (
        AdmissionBusyError,
        AdmissionClaim,
        AdmissionConflictError,
        AdmissionStore,
        AdmissionUnconfirmedError,
    )

# Publication is a run operation. Its module stays unloaded until a caller names
# one of these exports, so importing this facade (the helper's hello path) does
# not pay for it. Admission stays lazy for the same reason.
_LAZY_ADMISSION = frozenset(
    {
        "AdmissionBusyError",
        "AdmissionClaim",
        "AdmissionConflictError",
        "AdmissionStore",
        "AdmissionUnconfirmedError",
    }
)
_LAZY_PUBLICATION = frozenset({"CheckoutOutcome", "PublicationError", "PublishedRevision"})

__all__ = [
    "COMMIT_IDENTITY_EMAIL",
    "COMMIT_IDENTITY_NAME",
    "CONTROL_HELLO_MAX_BYTES",
    "DEFAULT_GIT_BACKEND",
    "DEFAULT_READY_TIMEOUT_SECONDS",
    "GIT_BACKEND_ENV",
    "INBOX_FORMAT",
    "LPRINT_LOGGER",
    "MAX_SOCKET_PATH_BYTES",
    "OBJECTIVES_NAME",
    "OPERATOR_KEY_NAME",
    "PORT_PLACEHOLDER",
    "PROJECT_SCHEMA_VERSION",
    "RUN_SCHEMA_VERSION",
    "AcceleratorVendor",
    "AdmissionBusyError",
    "AdmissionClaim",
    "AdmissionConflictError",
    "AdmissionStore",
    "AdmissionUnconfirmedError",
    "AgentRoleExecutionRecord",
    "AmbiguousTaskError",
    "AtomicWriteEffects",
    "AtomicWriteStream",
    "CatalogueInput",
    "CheckoutOutcome",
    "CliGitRepository",
    "CommitFault",
    "CommitOutcome",
    "CommitSubject",
    "Committed",
    "ComparisonInput",
    "ComparisonName",
    "ConfigurationRoot",
    "Conflict",
    "ConflictReason",
    "ControlInbox",
    "DecodeMemo",
    "Ended",
    "ExampleDomain",
    "FakeStateModels",
    "FakeStateStore",
    "FakeStateStores",
    "FileAdmissionStore",
    "FileControlInbox",
    "FrameworkSnapshotStatus",
    "Git",
    "GitBackend",
    "GitBackendError",
    "GitCommandError",
    "GitError",
    "GitFaultSink",
    "GitObjectId",
    "GitRemoteRepository",
    "GitRepository",
    "GitRepositoryFactory",
    "GitRunHistory",
    "GitSnapshotFile",
    "GitSnapshotPlan",
    "GitText",
    "GitTimeoutError",
    "GitTracker",
    "GitTrackerEvents",
    "HardwareInput",
    "InboxClosedError",
    "InboxEntry",
    "InvalidTaskDefinitionError",
    "InvalidTaskNameError",
    "LocalAtomicWriteEffects",
    "LocalStateStore",
    "ModelMaker",
    "NestedRepositoryError",
    "NullGitTrackerEvents",
    "ObjectiveAxisInput",
    "ObjectivesInput",
    "ObservationFault",
    "OperatorDialer",
    "OperatorKey",
    "OperatorKeyError",
    "OperatorSocketOutsideRootError",
    "OrchestrationDescriptor",
    "OrchestrationRunManifest",
    "ParetoInput",
    "PatchStyle",
    "Pathspec",
    "PrivateDirectoryError",
    "Project",
    "ProjectError",
    "ProjectGitIntegration",
    "ProjectLayoutError",
    "ProjectManifest",
    "ProjectNotInitializedError",
    "ProjectRootNotFoundError",
    "ProjectSandboxPaths",
    "ProjectStateError",
    "PublicationError",
    "PublishedRevision",
    "Pygit2GitRepository",
    "QuarantinedEnvelope",
    "RecordedRuns",
    "RejectedEntry",
    "RepositoryLocation",
    "RequestState",
    "RequestStatus",
    "Revision",
    "RunEnvironmentRecord",
    "RunExecutionRecord",
    "RunHistory",
    "RunLogger",
    "RunResourceRequest",
    "ServedModelInput",
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
    "StoreDurabilityEffects",
    "StoreFence",
    "StoreFile",
    "StoreRecord",
    "StoredEnvelope",
    "TaskDirectory",
    "TaskName",
    "TaskNotFoundError",
    "TasksRoot",
    "Unknown",
    "UnsafeProjectPathError",
    "WorkloadInput",
    "adoption_branch",
    "atomic_write_bytes",
    "canonical_project_root",
    "check_comparisons_are_distinct",
    "comparison_directory",
    "comparison_namespace",
    "create_operator_key",
    "decode_state_document",
    "ended_unapplied",
    "ensure_private_directory",
    "generate_run_id",
    "git_repositories",
    "is_project_state_path",
    "load_objectives",
    "open_git_repository",
    "operator_key_path",
    "pygit2_installed",
    "read_operator_key",
    "resume_directory",
    "run_branch",
    "select_git_backend",
    "strip_ansi",
    "user_runtime_root",
    "validate_request_id",
    "validate_run_id",
    "validate_socket_path",
    "validate_state_namespace",
]


def __getattr__(name: str) -> object:
    """Load heavy exports on first use.

    ``Pygit2GitRepository`` imports pygit2, which this package must not require
    just to be imported. Publication and admission types load with the first
    caller that names them, not with every import of this facade.
    """
    if name == "FileAdmissionStore":
        return import_module("vs_project._admission").FileAdmissionStore
    if name in _LAZY_ADMISSION:
        return getattr(import_module("vs_project.api.admissions"), name)
    if name == "Pygit2GitRepository":
        from vs_project._pygit2_git_repository import (  # noqa: PLC0415  # lint-waiver: LW-415571 [PLC0415]; the libgit2 implementation imports pygit2, which this package must not require just to be imported.
            Pygit2GitRepository,
        )

        return Pygit2GitRepository
    if name in _LAZY_PUBLICATION:
        value = getattr(import_module("vs_project._revision_publication"), name)
        globals()[name] = value
        return value
    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)
