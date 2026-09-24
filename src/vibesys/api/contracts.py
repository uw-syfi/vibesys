"""Public contracts for `vibesys.api`: DTOs, enums, and the event sink.

No behavior lives here. Types that already exist in vibesys core are
re-exported instead of duplicated. `WorkspaceHandle` is deliberately not a
type defined here: a run's workspace is expressed as `vs_sandbox.HostResource`
(see `vibesys.api.session.RunWorkspace`) to avoid a lib -> core cycle.
"""

from __future__ import annotations

import warnings
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol

from vibesys.config import Config
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.events import CoreEvent, EventStatus
from vibesys.orchestration.environment import AgentEnvironment
from vibesys.orchestration.request import OrchestrationRunRequest, ResumeRef, RunRequestLike
from vibesys.orchestration.view import RunResult, RunStatus, RunView
from vs_agent.api import MCPServerSpec
from vs_project.api import OrchestrationDescriptor

if TYPE_CHECKING:
    from vibesys.loops.legacy_request import LoopKind, RunRequest

# Objective/MetricSpace live in vibesys.loops.metrics because the
# metric-comparison logic they carry is loop code.
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.schemas import CandidateDisposition, PerfDeltaReason

__all__ = [
    "AgentEnvironment",
    "CandidateDisposition",
    "Config",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreEvent",
    "EventSink",
    "EventStatus",
    "LoopKind",
    "MCPServerSpec",
    "MetricSpace",
    "Objective",
    "OrchestrationDescriptor",
    "OrchestrationRunRequest",
    "PerfDeltaReason",
    "ResumeRef",
    "RunRequest",
    "RunRequestLike",
    "RunResult",
    "RunStatus",
    "RunView",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Resolve deprecated built-in request names on explicit access."""
    if name not in {"LoopKind", "RunRequest"}:
        raise AttributeError(name)
    warnings.warn(
        f"vibesys.api.contracts.{name} is deprecated for new policies; use OrchestrationRunRequest",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(import_module("vibesys.loops.legacy_request"), name)


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
