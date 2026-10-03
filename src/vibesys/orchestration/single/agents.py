"""Fixed agent declarations owned by the single-agent orchestration."""

from vibesys.orchestration.single.prompts import render_system_prompt
from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    WorkspaceAccess,
)

DESIGNER = AgentRole(
    id="orchestrator",
    system_prompt=render_system_prompt("orchestrator"),
    workspace_access=WorkspaceAccess.LIMITED,
    required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
)

IMPLEMENTER = AgentRole(
    id="implementer",
    system_prompt=render_system_prompt("implementer"),
    workspace_access=WorkspaceAccess.READ_WRITE,
    required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
)

AGENTS = (DESIGNER, IMPLEMENTER)

__all__ = ["AGENTS", "DESIGNER", "IMPLEMENTER"]
