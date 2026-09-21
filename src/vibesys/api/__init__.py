"""The public surface of VibeSys core: the only module other packages import.

Everything else under `vibesys.*` is private to core + `entrypoints`. `server.*`
talks to core only through this module: no deep imports, no escape hatch.
`entrypoints`, as the composition root that assembles a `RunRequest`, still
reaches some core internals directly.

Most symbols here are contracts and Protocols; the rest are re-exports of
core-owned types that `server.*` legitimately needs (events, control signals,
the resource-handoff seam) so it never has to import their private home modules.
"""

from __future__ import annotations

from vibesys.agent_spec_config import agent_spec_from_config
from vibesys.api._readmodel import project_committed_run_view
from vibesys.api.contracts import (
    AgentEnvironment,
    CandidateDisposition,
    Config,
    ConfigurationDiagnostic,
    ConfigurationError,
    CoreEvent,
    EventSink,
    EventStatus,
    HypothesisRoundView,
    HypothesisView,
    LoopKind,
    MetricSpace,
    Objective,
    PerfDeltaReason,
    ResumeRef,
    RoundView,
    RunRequest,
    RunResult,
    RunStatus,
    RunView,
)
from vibesys.api.entry import default_request, load_config, validate
from vibesys.api.session import (
    RunAgentHost,
    RunControl,
    RunQuery,
    RunSession,
    RunWorkspace,
    create_session,
)
from vibesys.api.store import RunStore, open_run_store
from vibesys.constants import KNOWN_COMPUTE_BACKENDS, ComputeBackend, DomainName
from vibesys.events import AgentExecutionStartedData, CoreEventType
from vibesys.loops.agent.issue_board import framework_memory_paths
from vibesys.profilers import ProfilerKind
from vibesys.render.sink import output_sink
from vibesys.repository import RepositoryVisibility
from vibesys.run import RunLogger
from vibesys.run.integration import RunResourceHandoff
from vibesys.run.run_control import RunStopped

__all__ = [
    "KNOWN_COMPUTE_BACKENDS",
    "AgentEnvironment",
    "AgentExecutionStartedData",
    "CandidateDisposition",
    "ComputeBackend",
    "Config",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreEvent",
    "CoreEventType",
    "DomainName",
    "EventSink",
    "EventStatus",
    "HypothesisRoundView",
    "HypothesisView",
    "LoopKind",
    "MetricSpace",
    "Objective",
    "PerfDeltaReason",
    "ProfilerKind",
    "RepositoryVisibility",
    "ResumeRef",
    "RoundView",
    "RunAgentHost",
    "RunControl",
    "RunLogger",
    "RunQuery",
    "RunRequest",
    "RunResourceHandoff",
    "RunResult",
    "RunSession",
    "RunStatus",
    "RunStopped",
    "RunStore",
    "RunView",
    "RunWorkspace",
    "agent_spec_from_config",
    "create_session",
    "default_request",
    "framework_memory_paths",
    "load_config",
    "open_run_store",
    "output_sink",
    "project_committed_run_view",
    "validate",
]
