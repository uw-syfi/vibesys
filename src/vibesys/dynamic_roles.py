"""The agent roles of a dynamic run on the core path.

The search declares each role once (`orchestration/dynamic/search/_roles`): the template, the
reply, the workspace, the time and the corrections. A role here is what only the host can add:
the system prompt read from a template file, the capabilities the provider must have and the
tools the agent is given. Each role's id comes from the search's id scheme, so the roles the
search asks and the roles the host declares cannot drift apart.

The implementer and the judge carry the bridged evaluation tool; no other role carries a tool,
so none needs an MCP server. The merger writes its checkout of main and measures nothing
itself: the search measures its commit with the trusted evaluations.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.core_roles import (
    CONTINUING_READER,
    MEASURING_READER,
    MEASURING_WRITER,
    READER,
    WRITER,
    RoleKind,
    core_role,
)
from vibesys.orchestration.dynamic.core_policy.api import prompts
from vibesys.orchestration.dynamic.search import api as search
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from vs_core.api.search import Role
    from vs_runtime.api import AgentRole

_RENDERER = TemplateRenderer(Path(prompts.__file__).parent)


def _role(role: Role, kind: RoleKind) -> AgentRole:
    return core_role(
        _RENDERER,
        role_id=search.dynamic.ids.role_id(role.name).root,
        system_template=f"{role.name}_system.j2",
        kind=kind,
    )


ORCHESTRATOR = _role(search.PLANNER, READER)
IMPLEMENTER = _role(search.IMPLEMENTER, MEASURING_WRITER)
JUDGE = _role(search.JUDGE, MEASURING_READER)
PROFILER = _role(search.PROFILER, CONTINUING_READER)
MERGER = _role(search.MERGER, WRITER)

CORE_ROLES = (ORCHESTRATOR, IMPLEMENTER, JUDGE, PROFILER, MERGER)
