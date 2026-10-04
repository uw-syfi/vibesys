"""Core runtime composition surface. No legacy loop or concrete I/O binding.

Start validates schema/declarations and persists new-epoch recovery. Inputs and
observations enter one queue; only confirmed commits authorize dispatch or
publication. Unbound executor roles return named typed refusals.
"""

from vs_runtime._core_loop import (
    CoreRuntime,
    CoreRuntimeBindings,
    CoreTransitions,
    DispatchProgress,
    ProductionCoreTransitions,
    PublicationDelivery,
    RuntimeCommitError,
    RuntimeCommitUncertainError,
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
    PublicationContext,
    PublicationHistory,
    RuntimeRecord,
)
from vs_runtime._core_requests import (
    REQUEST_DISPATCH,
    EvaluationRequests,
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    ExecutorRefusal,
    ExecutorRole,
    OperationExecutor,
    RefusingRequestExecution,
    RequestExecutors,
    SemanticEvents,
    SessionRequests,
    WorkspaceRequests,
)

__all__ = [
    "REQUEST_DISPATCH",
    "RUNTIME_SCHEMA_VERSION",
    "CoreResumeError",
    "CoreRuntime",
    "CoreRuntimeBindings",
    "CoreTransitions",
    "DispatchProgress",
    "EvaluationRequests",
    "ExecutionContext",
    "ExecutionOutcome",
    "ExecutionResult",
    "ExecutorRefusal",
    "ExecutorRole",
    "JournalPublicationDelivery",
    "OperationExecutor",
    "ProductionCoreTransitions",
    "Publication",
    "PublicationContext",
    "PublicationDelivery",
    "PublicationHistory",
    "RefusingRequestExecution",
    "RequestExecutors",
    "ResolvedCoreResume",
    "ResumeDiagnostic",
    "RuntimeCommitError",
    "RuntimeCommitUncertainError",
    "RuntimeRecord",
    "SemanticEvents",
    "SessionRequests",
    "WorkspaceRequests",
    "resolve_core_resume",
]
