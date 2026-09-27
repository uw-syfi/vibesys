"""Public contracts for `vibesys.api`: DTOs, enums, and the event sink.

No behavior lives here. Types that already exist in vibesys core are
re-exported instead of duplicated.
"""

from __future__ import annotations

from typing import Protocol

from vibesys.config import Config
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.events import CoreEvent, EventStatus

# Objective/MetricSpace are shared evaluator contracts.
from vibesys.orchestration.metrics import MetricSpace, Objective
from vibesys.run.contracts import ResumeRef, RunRequest, RunResult, RunStatus, RunView
from vs_agent.api import MCPServerSpec
from vs_loop_state.api import CandidateDisposition, PerfDeltaReason
from vs_project.api import OrchestrationDescriptor

__all__ = [
    "CandidateDisposition",
    "Config",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreEvent",
    "EventSink",
    "EventStatus",
    "MCPServerSpec",
    "MetricSpace",
    "Objective",
    "OrchestrationDescriptor",
    "PerfDeltaReason",
    "ResumeRef",
    "RunRequest",
    "RunResult",
    "RunStatus",
    "RunView",
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
