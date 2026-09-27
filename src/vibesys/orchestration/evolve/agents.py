"""Fixed agent declarations for evolutionary search."""

from vs_runtime.api import AgentRole, AgentTool, WorkspaceAccess

SHELL = AgentTool(id="shell")
PROFILER_TOOL = AgentTool(id="profiler")

MUTATOR = AgentRole(
    id="implementer",
    system_prompt=(
        "Edit the workspace to produce an offspring of the parent. "
        "Then return one JSON object matching the schema above."
    ),
    tools=(SHELL,),
    workspace_access=WorkspaceAccess.READ_WRITE,
)

JUDGE = AgentRole(
    id="judge",
    system_prompt=(
        "Review the implementation per the criteria above. Return only the JSON verdict."
    ),
    tools=(SHELL,),
    workspace_access=WorkspaceAccess.READ_ONLY,
)

PROFILER = AgentRole(
    id="profiler",
    system_prompt=(
        "Profile the server and return exactly one JSON object matching the schema above."
    ),
    tools=(SHELL, PROFILER_TOOL),
    workspace_access=WorkspaceAccess.READ_ONLY,
)

AGENTS = (MUTATOR, JUDGE, PROFILER)

__all__ = ["AGENTS", "JUDGE", "MUTATOR", "PROFILER"]
