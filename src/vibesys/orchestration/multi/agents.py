"""Fixed agent declarations owned by the multi-agent orchestration."""

from vs_runtime.api import AgentCapability, AgentRole, AgentTool, WorkspaceAccess

DESIGNER = AgentRole(
    id="orchestrator",
    system_prompt=(
        "You coordinate an evidence-driven optimization campaign. Decide when "
        "specialist profiling is useful, then design one bounded hypothesis at a "
        "time. Return only the requested structured response."
    ),
    workspace_access=WorkspaceAccess.LIMITED,
    required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
)

PROFILER = AgentRole(
    id="profiler",
    system_prompt=(
        "You collect and interpret bounded profiling evidence without changing "
        "candidate source or configuration. Return only the requested structured response."
    ),
    workspace_access=WorkspaceAccess.LIMITED,
    extra_tools=(AgentTool(id="profiler"),),
)

IMPLEMENTER = AgentRole(
    id="implementer",
    system_prompt=(
        "You implement the active optimization hypothesis, preserve reproducible "
        "evidence, and report the exact observed outcome. Return only the requested "
        "structured response."
    ),
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
)

JUDGE = AgentRole(
    id="judge",
    system_prompt=(
        "You independently audit the candidate and its evidence without editing the "
        "workspace. Return only the requested structured verdict."
    ),
    workspace_access=WorkspaceAccess.READ_ONLY,
)

AGENTS = (DESIGNER, PROFILER, IMPLEMENTER, JUDGE)

__all__ = ["AGENTS", "DESIGNER", "IMPLEMENTER", "JUDGE", "PROFILER"]
