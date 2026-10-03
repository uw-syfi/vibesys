"""Fixed agent declarations for evolutionary search."""

from vibesys.orchestration.evolve.prompts import render_system_prompt
from vs_runtime.api import AgentRole, AgentTool, WorkspaceAccess

PROFILER_TOOL = AgentTool(id="profiler")

MUTATOR = AgentRole(
    id="implementer",
    system_prompt=render_system_prompt("mutator"),
    workspace_access=WorkspaceAccess.READ_WRITE,
)

JUDGE = AgentRole(
    id="judge",
    system_prompt=render_system_prompt("judge"),
    workspace_access=WorkspaceAccess.READ_ONLY,
)

PROFILER = AgentRole(
    id="profiler",
    system_prompt=render_system_prompt("profiler"),
    extra_tools=(PROFILER_TOOL,),
    workspace_access=WorkspaceAccess.READ_ONLY,
)

AGENTS = (MUTATOR, JUDGE, PROFILER)

__all__ = ["AGENTS", "JUDGE", "MUTATOR", "PROFILER"]
