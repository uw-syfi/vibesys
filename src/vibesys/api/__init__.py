"""The public surface of VibeSys core: the only module other packages import.

Everything else under `vibesys.*` is private to core. `server.*` and
`entrypoints` talk to core only through this module and its `request` submodule:
no deep imports, no escape hatch. This module is the run/observe contract (build
a session from a `RunRequest`, then read back its events and views);
`vibesys.api.request` is the surface for assembling a `RunRequest`.

Most symbols here are contracts and Protocols; the rest are re-exports of
core-owned types that consumers legitimately need (events, control signals,
the resource-handoff seam) so they never import their private home modules.
"""

from __future__ import annotations

from vibesys import boot_trace
from vibesys.agent_spec_config import agent_spec_from_config
from vibesys.api._agent_state import agent_run_objectives, is_agent_run_manifest
from vibesys.api._orchestrations.builtins import built_in_orchestrations
from vibesys.api._orchestrations.contracts import Orchestration, OrchestrationRegistry
from vibesys.api.contracts import (
    Config,
    ConfigurationDiagnostic,
    ConfigurationError,
    CoreEvent,
    EventStatus,
    HypothesisRoundView,
    HypothesisView,
    LoopKind,
    MetricSpace,
    Objective,
    OrchestrationDescriptor,
    PerfDeltaReason,
    ResumeRef,
    RunRequest,
    RunResult,
    RunStatus,
    RunView,
)
from vibesys.api.entry import load_config
from vibesys.api.session import RunControl, RunSession, create_session
from vibesys.api.store import RunStore, open_run_store
from vibesys.constants import KNOWN_COMPUTE_BACKENDS, ComputeBackend, DomainName
from vibesys.events import (
    AgentExecutionStartedData,
    AgentOutputChunkData,
    CoreEventType,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
)
from vibesys.loops.agent.issue_board import framework_memory_paths
from vibesys.profilers import ProfilerKind
from vibesys.render.format import format_status_prefix
from vibesys.render.run_log import format_framework_event
from vibesys.render.sink import output_sink
from vibesys.repository import RepositoryVisibility
from vibesys.run.integration import RunResourceHandoff
from vibesys.run.run_control import RunStopped

__all__ = [
    "KNOWN_COMPUTE_BACKENDS",
    "AgentExecutionStartedData",
    "AgentOutputChunkData",
    "ComputeBackend",
    "Config",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreEvent",
    "CoreEventType",
    "DomainName",
    "EventStatus",
    "HypothesisRoundView",
    "HypothesisView",
    "LoopKind",
    "MetricSpace",
    "Objective",
    "Orchestration",
    "OrchestrationDescriptor",
    "OrchestrationRegistry",
    "PerfDeltaReason",
    "ProfilerKind",
    "RepositoryVisibility",
    "ResumeRef",
    "RunControl",
    "RunRequest",
    "RunResourceHandoff",
    "RunResult",
    "RunSession",
    "RunStatus",
    "RunStopped",
    "RunStore",
    "RunView",
    "TodoItemData",
    "TodoUpdateData",
    "ToolCallData",
    "ToolResultData",
    "agent_run_objectives",
    "agent_spec_from_config",
    "boot_trace",
    "built_in_orchestrations",
    "create_session",
    "format_framework_event",
    "format_status_prefix",
    "framework_memory_paths",
    "is_agent_run_manifest",
    "load_config",
    "open_run_store",
    "output_sink",
]
