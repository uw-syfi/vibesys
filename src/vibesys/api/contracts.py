"""Public contracts for `vibesys.api`: DTOs, enums, and the event sink.

No behavior lives here. Types that already exist in vibesys core are
re-exported instead of duplicated.
"""

from __future__ import annotations

from typing import Protocol

from vibesys.config import Config
from vibesys.errors import (
    ArgumentsRejected,
    ArgumentsUnrecognized,
    ConfigInvalid,
    ConfigurationCause,
    ConfigurationDiagnostic,
    ConfigurationError,
    GitHubUnavailable,
    ModelNotVetted,
    RolesNotDeclared,
    TaskImageUnsupported,
    TaskNeedsProvisioning,
    TaskNotFound,
    TaskUnusable,
    cli_wording,
    configuration_error,
)
from vibesys.events import CoreEvent, EventStatus, RunFailure, RunFailureKind
from vibesys.run.contracts import (
    IterationKind,
    IterationMetric,
    IterationProjection,
    IterationStatus,
    IterationSummary,
    IterationSupport,
    IterationTree,
    IterationUnavailable,
    IterationUnavailableReason,
    PluginProjection,
    ProfilerKind,
    ResumeRef,
    RunRequest,
    RunResult,
    RunStatus,
    RunView,
)
from vs_agent.api import MCPServerSpec
from vs_project.api import OrchestrationDescriptor

__all__ = [
    "ArgumentsRejected",
    "ArgumentsUnrecognized",
    "Config",
    "ConfigInvalid",
    "ConfigurationCause",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreEvent",
    "EventSink",
    "EventStatus",
    "GitHubUnavailable",
    "IterationKind",
    "IterationMetric",
    "IterationProjection",
    "IterationStatus",
    "IterationSummary",
    "IterationSupport",
    "IterationTree",
    "IterationUnavailable",
    "IterationUnavailableReason",
    "MCPServerSpec",
    "ModelNotVetted",
    "OrchestrationDescriptor",
    "PluginProjection",
    "ProfilerKind",
    "ResumeRef",
    "RolesNotDeclared",
    "RunFailure",
    "RunFailureKind",
    "RunRequest",
    "RunResult",
    "RunStatus",
    "RunView",
    "TaskImageUnsupported",
    "TaskNeedsProvisioning",
    "TaskNotFound",
    "TaskUnusable",
    "cli_wording",
    "configuration_error",
]


class EventSink(Protocol):
    """Receives the semantic core event stream for one run.

    Matches the duck-typed subscriber shape used by
    `vibesys.run.event_journal.EventSubscriber`
    (`Callable[[CoreEvent], None]`): any plain function or bound method with
    this signature satisfies it, including a headless renderer's bound
    `.handle` method.
    """

    def __call__(self, event: CoreEvent) -> None:
        """Handle one emitted core event."""
        ...
