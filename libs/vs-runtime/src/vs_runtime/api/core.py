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
from vs_runtime._evaluation_requests import JobRecord, MeasurementRequests
from vs_runtime._observation_factory import (
    ObservationFactory,
    ObservationFacts,
    ObservationLedgerCorruptError,
    ObservationSubject,
)
from vs_runtime._operation_catalog import (
    Applied,
    CancellableOwner,
    Cancelled,
    Indeterminate,
    Inspection,
    NotApplied,
    OperationCatalog,
    OperationEntry,
    OperationOwner,
    RefusalReason,
)
from vs_runtime._operation_receipts import (
    IntentReceipt,
    NamespaceOperationReceipts,
    OperationReceipts,
    ResultReceipt,
)
from vs_runtime._operation_requests import RegisteredOperationRequests
from vs_runtime._operation_wiring import OperationRole, bind_operations, build_operation_catalog
from vs_runtime._receipt_store import ReceiptStore
from vs_runtime._render_operation import RenderArtifactsOwner
from vs_runtime._semantic_events import BlockDiagnostic, JournalSemanticEvents
from vs_runtime._verify_revision_operation import VerifyRevisionOwner
from vs_runtime._workspace_receipts import (
    AttemptBinding,
    ExecutionRecord,
    NamespaceWorkspaceReceipts,
    ReceiptCorruptError,
    ReceiptPhase,
    RootGrant,
    WorkspaceReceipts,
)
from vs_runtime._workspace_requests import RuntimeWorkspaceRequests, commit_of, revision_ref

__all__ = [
    "REQUEST_DISPATCH",
    "RUNTIME_SCHEMA_VERSION",
    "Applied",
    "AttemptBinding",
    "BlockDiagnostic",
    "CancellableOwner",
    "Cancelled",
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
    "Indeterminate",
    "Inspection",
    "IntentReceipt",
    "JobRecord",
    "JournalPublicationDelivery",
    "JournalSemanticEvents",
    "MeasurementRequests",
    "NamespaceOperationReceipts",
    "NamespaceWorkspaceReceipts",
    "NotApplied",
    "ObservationFactory",
    "ObservationFacts",
    "ObservationLedgerCorruptError",
    "ObservationRejectedError",
    "ObservationSubject",
    "OperationCatalog",
    "OperationEntry",
    "OperationExecutor",
    "OperationOwner",
    "OperationReceipts",
    "OperationRole",
    "OwnerEvent",
    "ProductionCoreTransitions",
    "Publication",
    "PublicationAcknowledgement",
    "PublicationContext",
    "PublicationDelivery",
    "PublicationHistory",
    "ReceiptCorruptError",
    "ReceiptPhase",
    "ReceiptStore",
    "RefusalReason",
    "RefusingRequestExecution",
    "RegisteredOperationRequests",
    "RenderArtifactsOwner",
    "RequestExecutors",
    "ResolvedCoreResume",
    "ResultReceipt",
    "ResumeDiagnostic",
    "RootGrant",
    "RuntimeCommitError",
    "RuntimeCommitUncertainError",
    "RuntimeExecutionError",
    "RuntimeRecord",
    "RuntimeWorkspaceRequests",
    "SemanticEvents",
    "SessionRequests",
    "VerifyRevisionOwner",
    "WorkspaceReceipts",
    "WorkspaceRequests",
    "bind_operations",
    "build_operation_catalog",
    "commit_of",
    "resolve_core_resume",
    "revision_ref",
]
