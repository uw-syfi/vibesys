"""The public surface of VibeSys core: the only module other packages import.

Everything else under `vibesys.*` is private to core. `server.*` and
`entrypoints` talk to core only through this module and its `request` submodule:
no deep imports, no escape hatch. This module is the run/observe contract (build
a session from a `RunRequest`, then read back its events and views);
`vibesys.api.request` is the surface for assembling a `RunRequest`.

Most symbols here are contracts and Protocols; the rest are re-exports of
core-owned types that consumers legitimately need, so they never import their
private home modules.
"""

from __future__ import annotations

from vibesys.agent_spec_config import agent_spec_from_config
from vibesys.api.auxiliary import (
    AuxiliaryAgentLaunch,
    AuxiliaryReadableInput,
    ManagedAgent,
    RunReady,
)
from vibesys.api.contracts import (
    Config,
    ConfigurationDiagnostic,
    ConfigurationError,
    CoreEvent,
    EventStatus,
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
from vibesys.api.store import (
    RunStore,
    open_run_store,
)
from vibesys.api.store import (
    portable_history_snapshots as _portable_history_snapshots,  # noqa: F401  # lint-waiver: LW-020001 [F401]; server.controller imports this private facade helper by name, so the alias is a deliberate re-export.
)
from vibesys.constants import KNOWN_COMPUTE_BACKENDS, ComputeBackend, DomainName
from vibesys.events import (
    AgentExecutionStartedData,
    AgentOutputChunkData,
    AgentStatusData,
    CoreEventType,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
)
from vibesys.orchestration.contracts import OrchestrationRegistry
from vibesys.profilers import ProfilerKind
from vibesys.repository import RepositoryVisibility
from vibesys.run import CoreAgentEventSink
from vibesys.run.log_projection import format_framework_event
from vs_agent.api import AgentBackend, AgentSpec
from vs_runtime.api import boot_trace
from vs_runtime.api.infrastructure import RunStopped

__all__ = [
    "KNOWN_COMPUTE_BACKENDS",
    "AgentBackend",
    "AgentExecutionStartedData",
    "AgentOutputChunkData",
    "AgentSpec",
    "AgentStatusData",
    "AuxiliaryAgentLaunch",
    "AuxiliaryReadableInput",
    "ComputeBackend",
    "Config",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreAgentEventSink",
    "CoreEvent",
    "CoreEventType",
    "DomainName",
    "EventStatus",
    "ManagedAgent",
    "MetricSpace",
    "Objective",
    "OrchestrationDescriptor",
    "OrchestrationRegistry",
    "PerfDeltaReason",
    "ProfilerKind",
    "RepositoryVisibility",
    "ResumeRef",
    "RunControl",
    "RunReady",
    "RunRequest",
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
    "agent_spec_from_config",
    "boot_trace",
    "create_session",
    "format_framework_event",
    "load_config",
    "open_run_store",
]
