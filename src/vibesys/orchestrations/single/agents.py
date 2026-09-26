"""Fixed agent declarations owned by the single-agent orchestration."""

from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentTool,
    WorkspaceAccess,
)

SHELL = AgentTool(id="shell")

DESIGNER = AgentRole(
    id="orchestrator",
    system_prompt=(
        "You design one evidence-driven optimization hypothesis at a time. "
        "Keep hypothesis identifiers stable, do not update the hypothesis you are "
        "creating, and return only the requested structured response."
    ),
    tools=(SHELL,),
    workspace_access=WorkspaceAccess.READ_ONLY,
    required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
)

IMPLEMENTER = AgentRole(
    id="implementer",
    system_prompt=(
        "You implement one optimization plan end-to-end, check correctness, and "
        "self-review the result. Work only in the assigned workspace and return "
        "only the requested structured response."
    ),
    tools=(SHELL,),
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
)

AGENTS = (DESIGNER, IMPLEMENTER)

__all__ = ["AGENTS", "DESIGNER", "IMPLEMENTER"]
