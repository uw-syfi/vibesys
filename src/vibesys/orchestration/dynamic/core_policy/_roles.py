"""The agent roles of a dynamic run on the core path.

A core turn ends with a typed reply that the strategy folds, and measurement is the
strategy's own decision, so these roles carry no agent tools and need no MCP server. Each
role's id is the strategy's (`role_id`), so the roles the strategy names and the roles the
host declares cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path

from vibesys.orchestration.dynamic.core_policy import prompts
from vibesys.orchestration.dynamic.strategy.api import Role, role_id
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import AgentCapability, AgentRole, WorkspaceAccess

_RENDERER = TemplateRenderer(Path(prompts.__file__).parent)

# A write turn that ends in a reply the host may re-dispatch needs the provider to resume it.
_CONTINUATION = frozenset(
    {
        AgentCapability.SESSION_REUSE,
        AgentCapability.PROVIDER_SESSION_RESUME,
        AgentCapability.DURABLE_TURN_CONTINUATION,
    }
)


def _role(
    role: Role, access: WorkspaceAccess, capabilities: frozenset[AgentCapability]
) -> AgentRole:
    return AgentRole(
        id=role_id(role.value).root,
        system_prompt=_RENDERER.render_template(f"{role.value}_system.j2"),
        workspace_access=access,
        required_capabilities=capabilities,
    )


ORCHESTRATOR = _role(Role.PLANNER, WorkspaceAccess.READ_ONLY, frozenset())
IMPLEMENTER = _role(Role.IMPLEMENTER, WorkspaceAccess.READ_WRITE, _CONTINUATION)
JUDGE = _role(Role.JUDGE, WorkspaceAccess.READ_ONLY, _CONTINUATION)
PROFILER = _role(Role.PROFILER, WorkspaceAccess.READ_ONLY, _CONTINUATION)

CORE_ROLES = (ORCHESTRATOR, IMPLEMENTER, JUDGE, PROFILER)
