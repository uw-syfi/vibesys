"""The agent roles of a dynamic run on the core path.

A core turn ends with a typed reply that the strategy folds. The implementer and the judge
carry the bridged evaluation tool; no other role carries a tool, so none needs an MCP server. Each
role's id is the strategy's (`role_id`), so the roles the strategy names and the roles the
host declares cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path

from vibesys.orchestration.dynamic.core_policy import prompts
from vibesys.orchestration.dynamic.strategy.api import Role, role_id
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import AgentCapability, AgentRole, AgentTool, WorkspaceAccess
from vs_runtime.api.core import EVALUATION_TOOL_ID

_RENDERER = TemplateRenderer(Path(prompts.__file__).parent)

# A turn that ends in a reply the host may re-dispatch needs the provider to resume it.
_CONTINUATION = frozenset(
    {
        AgentCapability.SESSION_REUSE,
        AgentCapability.PROVIDER_SESSION_RESUME,
        AgentCapability.DURABLE_TURN_CONTINUATION,
    }
)
# The implementer and the judge may also submit a measurement of their own workspace through
# the bridged evaluation tool; core decides admission and budget, so the tool has no status or
# wait. Reaching the tool server needs MCP.
_MEASURING = _CONTINUATION | {AgentCapability.MCP_SERVERS}
_EVALUATION = AgentTool(id=EVALUATION_TOOL_ID)


def _role(
    role: Role,
    access: WorkspaceAccess,
    capabilities: frozenset[AgentCapability],
    tools: tuple[AgentTool, ...] = (),
) -> AgentRole:
    return AgentRole(
        id=role_id(role.value).root,
        system_prompt=_RENDERER.render_template(f"{role.value}_system.j2"),
        extra_tools=tools,
        workspace_access=access,
        required_capabilities=capabilities,
    )


ORCHESTRATOR = _role(Role.PLANNER, WorkspaceAccess.READ_ONLY, frozenset())
IMPLEMENTER = _role(Role.IMPLEMENTER, WorkspaceAccess.READ_WRITE, _MEASURING, (_EVALUATION,))
JUDGE = _role(Role.JUDGE, WorkspaceAccess.READ_ONLY, _MEASURING, (_EVALUATION,))
PROFILER = _role(Role.PROFILER, WorkspaceAccess.READ_ONLY, _CONTINUATION)

CORE_ROLES = (ORCHESTRATOR, IMPLEMENTER, JUDGE, PROFILER)
