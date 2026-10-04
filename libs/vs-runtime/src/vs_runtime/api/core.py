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

__all__ = [
    "REQUEST_DISPATCH",
    "RUNTIME_SCHEMA_VERSION",
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
    "ExecutionResult",
    "ExecutorRefusal",
    "ExecutorRole",
    "JournalPublicationDelivery",
    "OperationExecutor",
    "OwnerEvent",
    "ProductionCoreTransitions",
    "Publication",
    "PublicationAcknowledgement",
    "PublicationContext",
    "PublicationDelivery",
    "PublicationHistory",
    "RefusingRequestExecution",
    "RequestExecutors",
    "ResolvedCoreResume",
    "ResumeDiagnostic",
    "RuntimeCommitError",
    "RuntimeCommitUncertainError",
    "RuntimeExecutionError",
    "RuntimeRecord",
    "SemanticEvents",
    "SessionRequests",
    "WorkspaceRequests",
    "resolve_core_resume",
]
