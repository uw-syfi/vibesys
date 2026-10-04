"""Core runtime composition surface. No legacy loop or concrete I/O binding.

Start validates schema/declarations and persists new-epoch recovery. Inputs and
observations enter one queue; only confirmed commits authorize dispatch or
publication. Unbound executor roles return named typed refusals.
"""

from vs_runtime._core_loop import (
    CoreRuntime,
    CoreRuntimeBindings,
    CoreTransitions,
    ProductionCoreTransitions,
    PublicationDelivery,
    RuntimeCommitError,
    RuntimeCommitUncertainError,
)
from vs_runtime._core_record import RUNTIME_SCHEMA_VERSION, Publication, RuntimeRecord
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
    "CoreRuntime",
    "CoreRuntimeBindings",
    "CoreTransitions",
    "EvaluationRequests",
    "ExecutionContext",
    "ExecutionOutcome",
    "ExecutionResult",
    "ExecutorRefusal",
    "ExecutorRole",
    "OperationExecutor",
    "ProductionCoreTransitions",
    "Publication",
    "PublicationDelivery",
    "RefusingRequestExecution",
    "RequestExecutors",
    "RuntimeCommitError",
    "RuntimeCommitUncertainError",
    "RuntimeRecord",
    "SemanticEvents",
    "SessionRequests",
    "WorkspaceRequests",
]
