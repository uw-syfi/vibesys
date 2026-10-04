"""Agent roles owned by the dynamic orchestration."""

from vibesys.orchestration.dynamic.prompts import render_system_prompt
from vs_runtime.api import AgentCapability, AgentRole, AgentTool, WorkspaceAccess

EVALUATION = AgentTool(id="evaluation")
PROFILER_TOOL = AgentTool(id="profiler")

_EVALUATION_CONTINUATION_CAPABILITIES = frozenset(
    {
        AgentCapability.MCP_SERVERS,
        AgentCapability.SESSION_REUSE,
        AgentCapability.PROVIDER_SESSION_RESUME,
        AgentCapability.DURABLE_TURN_CONTINUATION,
    }
)

ORCHESTRATOR = AgentRole(
    id="dynamic-orchestrator",
    system_prompt=render_system_prompt("orchestrator"),
    extra_tools=(EVALUATION,),
    workspace_access=WorkspaceAccess.READ_ONLY,
    required_capabilities=frozenset({AgentCapability.MCP_SERVERS}),
)

IMPLEMENTER = AgentRole(
    id="dynamic-implementer",
    system_prompt=render_system_prompt("implementer"),
    extra_tools=(EVALUATION,),
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=_EVALUATION_CONTINUATION_CAPABILITIES,
)

JUDGE = AgentRole(
    id="dynamic-judge",
    system_prompt=render_system_prompt("judge"),
    extra_tools=(EVALUATION,),
    workspace_access=WorkspaceAccess.READ_ONLY,
    required_capabilities=_EVALUATION_CONTINUATION_CAPABILITIES,
)

PROFILER = AgentRole(
    id="dynamic-profiler",
    system_prompt=render_system_prompt("profiler"),
    extra_tools=(EVALUATION, PROFILER_TOOL),
    workspace_access=WorkspaceAccess.READ_ONLY,
    required_capabilities=_EVALUATION_CONTINUATION_CAPABILITIES,
)

AGENTS = (ORCHESTRATOR, IMPLEMENTER, JUDGE, PROFILER)

__all__ = [
    "AGENTS",
    "EVALUATION",
    "IMPLEMENTER",
    "JUDGE",
    "ORCHESTRATOR",
    "PROFILER",
    "PROFILER_TOOL",
]
