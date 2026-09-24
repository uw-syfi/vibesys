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

import warnings
from importlib import import_module
from typing import Any

from vibesys import boot_trace
from vibesys.agent_spec_config import agent_spec_from_config

# Deprecated public imports retained for existing callers; omitted from __all__.
from vibesys.api._orchestrations.contracts import (
    ExecutableOrchestration,
    OrchestrationRegistry,
)
from vibesys.api._orchestrations.contracts import (
    Orchestration as Orchestration,
)
from vibesys.api._orchestrations.contracts import (
    RunDescription as RunDescription,
)
from vibesys.api.contracts import (
    Config,
    ConfigurationDiagnostic,
    ConfigurationError,
    CoreEvent,
    EventStatus,
    LoopKind,
    MetricSpace,
    Objective,
    OrchestrationDescriptor,
    OrchestrationRunRequest,
    PerfDeltaReason,
    ResumeRef,
    RunRequest,
    RunRequestLike,
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
    portable_history_snapshots as _portable_history_snapshots,  # noqa: F401
)
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
from vibesys.profilers import ProfilerKind
from vibesys.render.format import format_status_prefix
from vibesys.render.run_log import format_framework_event
from vibesys.render.sink import output_sink
from vibesys.repository import RepositoryVisibility
from vibesys.run.integration import RunResourceHandoff
from vibesys.run.run_control import RunStopped
from vibesys.runtime import AgentDefinition, AgentHandle, VibeSysRuntime
from vs_agent.api import AgentBackend, AgentSpec
from vs_sandbox.api import HostResource, HostResourceAccess


def built_in_orchestrations() -> OrchestrationRegistry:
    """Construct the built-in registry only when a caller selects it."""
    from vibesys.api._orchestrations.builtins import (  # noqa: PLC0415
        built_in_orchestrations as create_builtin_registry,
    )

    return create_builtin_registry()


__all__ = [
    "KNOWN_COMPUTE_BACKENDS",
    "AgentBackend",
    "AgentDefinition",
    "AgentExecutionStartedData",
    "AgentHandle",
    "AgentOutputChunkData",
    "AgentRunProjection",
    "AgentSpec",
    "ComputeBackend",
    "Config",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreEvent",
    "CoreEventType",
    "DomainName",
    "EventStatus",
    "ExecutableOrchestration",
    "HostResource",
    "HostResourceAccess",
    "HypothesisRoundView",
    "HypothesisView",
    "LoopKind",
    "MetricSpace",
    "Objective",
    "OrchestrationDescriptor",
    "OrchestrationRegistry",
    "OrchestrationRunRequest",
    "PerfDeltaReason",
    "ProfilerKind",
    "RepositoryVisibility",
    "ResumeRef",
    "RunControl",
    "RunRequest",
    "RunRequestLike",
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
    "VibeSysRuntime",
    "agent_projection",
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

_AGENT_COMPAT_EXPORTS = frozenset(
    {
        "AgentRunProjection",
        "HypothesisRoundView",
        "HypothesisView",
        "agent_projection",
        "agent_run_objectives",
        "framework_memory_paths",
        "is_agent_run_manifest",
    }
)


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Resolve deprecated agent-only imports without loading them eagerly."""
    if name not in _AGENT_COMPAT_EXPORTS:
        raise AttributeError(name)
    warnings.warn(
        f"vibesys.api.{name} is deprecated; import it from vibesys.api.agent",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(import_module("vibesys.api.agent"), name)
