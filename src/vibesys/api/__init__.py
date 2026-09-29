"""The public surface of VibeSys core: the only module other packages import.

Everything else under `vibesys.*` is private to core. `server.*` and
`entrypoints` talk to core only through this package: the package root is the
policy-neutral run/observe contract, while explicitly named submodules expose
built-in policy projections and configuration. `vibesys.api.request` is the
surface for assembling a `RunRequest`.

Most symbols here are contracts and Protocols; the rest are re-exports of
core-owned types that consumers legitimately need, so they never import their
private home modules.
"""

from __future__ import annotations

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
    OrchestrationDescriptor,
    PluginProjection,
    ProfilerKind,
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
from vibesys.config import DOTENV_PATH
from vibesys.constants import KNOWN_COMPUTE_BACKENDS, ComputeBackend, DomainName
from vibesys.events import (
    AgentExecutionStartedData,
    AgentOutputChannel,
    AgentOutputChunkData,
    AgentStatusData,
    AsyncOperationKind,
    AsyncOperationLifecycleData,
    AsyncOperationState,
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
from vibesys.plugin_catalog import OrchestrationRegistry
from vibesys.repository import RepositoryVisibility
from vibesys.run import CoreAgentEventSink
from vs_agent.api import SUGGESTED_MODELS, AgentBackend, AgentSpec
from vs_runtime.api import boot_trace
from vs_runtime.api.infrastructure import RunStopped

__all__ = [
    "DOTENV_PATH",
    "KNOWN_COMPUTE_BACKENDS",
    "SUGGESTED_MODELS",
    "AgentBackend",
    "AgentDriver",
    "AgentExecutionStartedData",
    "AgentOutputChannel",
    "AgentOutputChunkData",
    "AgentSpec",
    "AgentStatusData",
    "AsyncOperationKind",
    "AsyncOperationLifecycleData",
    "AsyncOperationState",
    "AuxiliaryAgentDriver",
    "AuxiliaryAgentLaunch",
    "AuxiliaryReadableInput",
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
    "JsonResultPayload",
    "ManagedAgent",
    "OrchestrationDescriptor",
    "OrchestrationRegistry",
    "PluginProjection",
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
    "agent_spec_from_config",
    "boot_trace",
    "create_session",
    "load_config",
    "open_run_store",
]
