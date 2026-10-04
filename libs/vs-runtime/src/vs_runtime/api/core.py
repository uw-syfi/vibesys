"""Core runtime composition surface. No legacy loop or concrete I/O binding.

Start validates schema/declarations and persists new-epoch recovery. Inputs and
observations enter one queue; only confirmed commits authorize dispatch or
publication. Unbound executor roles return named typed refusals.
"""

from vs_runtime._core_loop import (
    CoreContractGapError,
    CoreRuntime,
    CoreRuntimeBindings,
    CoreTransitions,
    DispatchProgress,
    ObservationRejectedError,
    ProductionCoreTransitions,
    PublicationDelivery,
    RuntimeCommitError,
    RuntimeCommitUncertainError,
    RuntimeExecutionError,
)
from vs_runtime._core_preflight import (
    CoreResumeError,
    ResolvedCoreResume,
    ResumeDiagnostic,
    resolve_core_resume,
)
from vs_runtime._core_publications import JournalPublicationDelivery
from vs_runtime._core_record import (
    RUNTIME_SCHEMA_VERSION,
    Publication,
    PublicationAcknowledgement,
    PublicationContext,
    PublicationHistory,
    RuntimeRecord,
)
from vs_runtime._core_requests import (
    REQUEST_DISPATCH,
    EvaluationRequests,
    ExecutionContext,
    ExecutionLease,
    ExecutionOutcome,
    ExecutionResult,
    ExecutorRefusal,
    ExecutorRole,
    OperationExecutor,
    OwnerEvent,
    RefusingRequestExecution,
    RequestExecutors,
    SemanticEvents,
    SessionRequests,
    WorkspaceRequests,
)
from vs_runtime._observation_factory import (
    ObservationFactory,
    ObservationFacts,
    ObservationLedgerCorruptError,
)
from vs_runtime._workspace_receipts import (
    AttemptBinding,
    ExecutionRecord,
    NamespaceWorkspaceReceipts,
    ReceiptCorruptError,
    ReceiptPhase,
    RootGrant,
    WorkspaceReceipts,
)
from vs_runtime._workspace_requests import RuntimeWorkspaceRequests, revision_ref

__all__ = [
    "REQUEST_DISPATCH",
    "RUNTIME_SCHEMA_VERSION",
    "AttemptBinding",
    "CoreContractGapError",
    "CoreResumeError",
    "CoreRuntime",
    "CoreRuntimeBindings",
    "CoreTransitions",
    "DispatchProgress",
    "EvaluationRequests",
    "ExecutionContext",
    "ExecutionLease",
    "ExecutionOutcome",
    "ExecutionRecord",
    "ExecutionResult",
    "ExecutorRefusal",
    "ExecutorRole",
    "JournalPublicationDelivery",
    "NamespaceWorkspaceReceipts",
    "ObservationFactory",
    "ObservationFacts",
    "ObservationLedgerCorruptError",
    "ObservationRejectedError",
    "OperationExecutor",
    "OwnerEvent",
    "ProductionCoreTransitions",
    "Publication",
    "PublicationAcknowledgement",
    "PublicationContext",
    "PublicationDelivery",
    "PublicationHistory",
    "ReceiptCorruptError",
    "ReceiptPhase",
    "RefusingRequestExecution",
    "RequestExecutors",
    "ResolvedCoreResume",
    "ResumeDiagnostic",
    "RootGrant",
    "RuntimeCommitError",
    "RuntimeCommitUncertainError",
    "RuntimeExecutionError",
    "RuntimeRecord",
    "RuntimeWorkspaceRequests",
    "SemanticEvents",
    "SessionRequests",
    "WorkspaceReceipts",
    "WorkspaceRequests",
    "resolve_core_resume",
    "revision_ref",
]
