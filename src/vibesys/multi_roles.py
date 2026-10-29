"""The agent roles of a multi run on the core path.

The search declares each role once (`orchestration/multi/search/_roles`, the designer shared
with single in `serial_search`): the template, the reply, the workspace, the time and the
corrections. A role here is what only the host can add: the system prompt read from a template
file, the capabilities the provider must have and the tools the agent is given. The designer
(which the router is, asked another question) reads only, its roadmap changes travelling in its
reply; the profiler writes nothing in the project, its findings travelling in its reply, and
reads captures through the stateless profiler tool; the implementer edits its candidate and, like the judge that reads it,
carries the bridged evaluation tool. Each role's id comes from the search's id scheme, so the
roles the search asks and the roles the host declares cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.core_roles import (
    MEASURING_READER,
    MEASURING_WRITER,
    RoleKind,
    core_role,
)
from vibesys.orchestration.multi.core_policy.api import prompts
from vibesys.orchestration.multi.search import api as search
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import AgentCapability, AgentTool, WorkspaceAccess

if TYPE_CHECKING:
    from vs_core.api.search import Role
    from vs_runtime.api import AgentRole

_RENDERER = TemplateRenderer(Path(prompts.__file__).parent)

# The designer reads the run root and writes nothing: its roadmap changes travel in its reply.
DESIGNER_KIND = RoleKind(WorkspaceAccess.READ_ONLY, frozenset())
# The profiler writes nothing in the project: its findings travel in its reply. It analyses
# captures with the profiler's tool server, which holds no run state (so it is no second writer
# beside core).
PROFILER_KIND = RoleKind(
    WorkspaceAccess.READ_ONLY,
    frozenset({AgentCapability.MCP_SERVERS}),
    (AgentTool(id="profiler", stateless=True),),
)


def _role(role: Role, kind: RoleKind) -> AgentRole:
    return core_role(
        _RENDERER,
        role_id=search.multi.ids.role_id(role.name).root,
        system_template=f"{role.name}_system.j2",
        kind=kind,
    )


DESIGNER = _role(search.PLANNER, DESIGNER_KIND)
PROFILER = _role(search.PROFILER, PROFILER_KIND)
IMPLEMENTER = _role(search.IMPLEMENTER, MEASURING_WRITER)
JUDGE = _role(search.JUDGE, MEASURING_READER)

CORE_ROLES = (DESIGNER, PROFILER, IMPLEMENTER, JUDGE)
