"""Fixed agent declarations owned by the multi-agent orchestration."""

from vibesys.orchestration.multi.prompts import render_system_prompt
from vs_runtime.api import AgentCapability, AgentRole, AgentTool, WorkspaceAccess

DESIGNER = AgentRole(
    id="orchestrator",
    system_prompt=render_system_prompt("orchestrator"),
    workspace_access=WorkspaceAccess.LIMITED,
    required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
)

PROFILER = AgentRole(
    id="profiler",
    system_prompt=render_system_prompt("profiler"),
    workspace_access=WorkspaceAccess.LIMITED,
    extra_tools=(AgentTool(id="profiler"),),
)

IMPLEMENTER = AgentRole(
    id="implementer",
    system_prompt=render_system_prompt("implementer"),
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
)

JUDGE = AgentRole(
    id="judge",
    system_prompt=render_system_prompt("judge"),
    workspace_access=WorkspaceAccess.READ_ONLY,
)

AGENTS = (DESIGNER, PROFILER, IMPLEMENTER, JUDGE)

__all__ = ["AGENTS", "DESIGNER", "IMPLEMENTER", "JUDGE", "PROFILER"]
