"""Agent roles owned by the dynamic orchestration."""

from vs_runtime.api import AgentCapability, AgentRole, AgentTool, WorkspaceAccess

EVALUATION = AgentTool(id="evaluation")
PROFILER_TOOL = AgentTool(id="profiler")

ORCHESTRATOR = AgentRole(
    id="dynamic-orchestrator",
    system_prompt=(
        "Choose a small portfolio of distinct, falsifiable optimization hypotheses from "
        "the supplied evidence references. Use `trusted_operations` when recent host-owned "
        "evaluation or profiler outcomes may inform the next choice. Return only the requested "
        "structured response."
    ),
    extra_tools=(EVALUATION,),
    workspace_access=WorkspaceAccess.READ_ONLY,
    required_capabilities=frozenset({AgentCapability.MCP_SERVERS}),
)

IMPLEMENTER = AgentRole(
    id="dynamic-implementer",
    system_prompt=(
        "Own one optimization hypothesis. Inspect and edit its isolated candidate, use "
        "available tools when useful, and return compact evidence references."
    ),
    extra_tools=(EVALUATION,),
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=frozenset({AgentCapability.MCP_SERVERS, AgentCapability.SESSION_REUSE}),
)

JUDGE = AgentRole(
    id="dynamic-judge",
    system_prompt=(
        "Independently assess one candidate against its hypothesis and linked evidence. "
        "Do not edit candidate source. Return only the requested structured response."
    ),
    extra_tools=(EVALUATION,),
    workspace_access=WorkspaceAccess.READ_ONLY,
    required_capabilities=frozenset({AgentCapability.MCP_SERVERS, AgentCapability.SESSION_REUSE}),
)

PROFILER = AgentRole(
    id="dynamic-profiler",
    system_prompt=(
        "Collect and interpret the requested profile for one exact candidate revision. "
        "Do not edit candidate source. Return compact evidence references and a concise "
        "diagnosis."
    ),
    extra_tools=(EVALUATION, PROFILER_TOOL),
    workspace_access=WorkspaceAccess.READ_ONLY,
    required_capabilities=frozenset(
        {
            AgentCapability.MCP_SERVERS,
            AgentCapability.SESSION_REUSE,
            AgentCapability.PROVIDER_SESSION_RESUME,
        }
    ),
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
