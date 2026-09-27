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

from vibesys.api.agent import (
    AgentRunProjection,
    CandidateDisposition,
    HypothesisOutcome,
    HypothesisResolution,
    HypothesisRoundView,
    HypothesisView,
    JudgeVerdict,
    agent_projection,
)
from vibesys.api.auxiliary import (
    AgentDriver,
    AuxiliaryAgentDriver,
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
    RunDocument,
    RunRecord,
    RunRecordFacts,
    RunRecordReadError,
    RunStore,
    WorkspaceChange,
    WorkspaceChangeKind,
    open_run_store,
)
from vibesys.composition import agent_spec_from_config
from vibesys.constants import KNOWN_COMPUTE_BACKENDS, ComputeBackend, DomainName
from vibesys.events import (
    AgentExecutionStartedData,
    AgentOutputChannel,
    AgentOutputChunkData,
    AgentStatusData,
    CommandResultPayload,
    CoreEventType,
    FrameworkWarningData,
    GateFinishedData,
    GateStartedData,
    JsonResultPayload,
    RunConfiguredData,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    ToolResultPayload,
    WorkspaceSnapshotData,
)
from vibesys.orchestration.contracts import OrchestrationRegistry
from vibesys.orchestration.evolve.models import resolve_openevolve_options
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.repository import RepositoryVisibility
from vibesys.run import CoreAgentEventSink
from vs_agent.api import AgentBackend, AgentSpec
from vs_runtime.api import boot_trace
from vs_runtime.api.infrastructure import RunStopped

__all__ = [
    "KNOWN_COMPUTE_BACKENDS",
    "AgentBackend",
    "AgentDriver",
    "AgentExecutionStartedData",
    "AgentOutputChannel",
    "AgentOutputChunkData",
    "AgentRunProjection",
    "AgentSpec",
    "AgentStatusData",
    "AuxiliaryAgentDriver",
    "AuxiliaryAgentLaunch",
    "AuxiliaryReadableInput",
    "CandidateDisposition",
    "CommandResultPayload",
    "ComputeBackend",
    "Config",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreAgentEventSink",
    "CoreEvent",
    "CoreEventType",
    "DomainName",
    "EventStatus",
    "FrameworkWarningData",
    "GateFinishedData",
    "GateStartedData",
    "HypothesisOutcome",
    "HypothesisResolution",
    "HypothesisRoundView",
    "HypothesisView",
    "JsonResultPayload",
    "JudgeVerdict",
    "ManagedAgent",
    "MetricSpace",
    "Objective",
    "OrchestrationDescriptor",
    "OrchestrationRegistry",
    "PerfDeltaReason",
    "ProfilerKind",
    "RepositoryVisibility",
    "ResumeRef",
    "RunConfiguredData",
    "RunControl",
    "RunDocument",
    "RunReady",
    "RunRecord",
    "RunRecordFacts",
    "RunRecordReadError",
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
    "ToolResultPayload",
    "WorkspaceChange",
    "WorkspaceChangeKind",
    "WorkspaceSnapshotData",
    "agent_projection",
    "agent_spec_from_config",
    "boot_trace",
    "create_session",
    "load_config",
    "open_run_store",
    "resolve_openevolve_options",
]
