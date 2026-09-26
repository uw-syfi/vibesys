"""Public contracts for `vibesys.api`: DTOs, enums, and the event sink.

No behavior lives here. Types that already exist in vibesys core are
re-exported instead of duplicated. `WorkspaceHandle` is deliberately not a
type defined here: a run's workspace is expressed as `vs_sandbox.HostResource`
(see `vibesys.api.session.RunWorkspace`) to avoid a lib -> core cycle.
"""

from __future__ import annotations

from typing import Protocol

from framework.api import MCPServerSpec, OrchestrationDescriptor, RunResult, RunStatus, RunView
from vibesys.config import Config
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError

# Objective/MetricSpace are shared evaluator contracts.
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.events import CoreEvent, EventStatus
from vibesys.loops.state.api import (
    CandidateDisposition,
    HypothesisOutcome,
    HypothesisResolution,
    JudgeVerdict,
    PerfDeltaReason,
)
from vibesys.orchestration.environment import AgentEnvironment
from vibesys.orchestration.request import ResumeRef, RunRequest

__all__ = [
    "AgentEnvironment",
    "CandidateDisposition",
    "Config",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreEvent",
    "EventSink",
    "EventStatus",
    "HypothesisOutcome",
    "HypothesisResolution",
    "JudgeVerdict",
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

    Matches the duck-typed subscriber shape already used by
    `vibesys.render.sink.EventHandler` and
    `vibesys.run.event_journal.EventSubscriber`
    (`Callable[[CoreEvent], None]`): any plain function or bound method with
    this signature satisfies it, including a headless renderer's bound
    `.handle` method.
    """

    def __call__(self, event: CoreEvent) -> None:
        """Handle one emitted core event."""
        ...
