"""The public surface of VibeSys core: the only module other packages import.

Everything else under `vibesys.*` is private to core + `entrypoints`. Both
consumers -- `entrypoints` (headless) and `server.*` -- talk to core only
through this module: no deep imports, no escape hatch.

This package is additive scaffolding (Wave 1 of the `vibesys.api` boundary
effort, see the narrow-boundary plan). The contracts and Protocols below are
the target shape; several entry points still raise `NotImplementedError`
pending later waves that move behavior in from `entrypoints/headless.py` and
`server.*`. Nothing outside this package imports it yet.
"""

from __future__ import annotations

from vibesys.agents import build_agent_client
from vibesys.agents.factory import supported_cli_providers
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
    MCPServerSpec,
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
from vibesys.run import RunLogger

__all__ = [
    "AgentEnvironment",
    "CandidateDisposition",
    "Config",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreEvent",
    "EventSink",
    "EventStatus",
    "HypothesisRoundView",
    "HypothesisView",
    "LoopKind",
    "MCPServerSpec",
    "MetricSpace",
    "Objective",
    "PerfDeltaReason",
    "ResumeRef",
    "RoundView",
    "RunAgentHost",
    "RunControl",
    "RunLogger",
    "RunQuery",
    "RunRequest",
    "RunResult",
    "RunSession",
    "RunStatus",
    "RunStore",
    "RunView",
    "RunWorkspace",
    "build_agent_client",
    "create_session",
    "default_request",
    "load_config",
    "open_run_store",
    "project_committed_run_view",
    "supported_cli_providers",
    "validate",
]
