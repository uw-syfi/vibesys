"""The agent roles of a single run on the core path.

The search declares each role once (`orchestration/single/search/_roles`, the designer shared
with multi in `serial_search`): the template, the reply, the workspace, the time and the
corrections. A role here is what only the host can add: the system prompt read from a template
file, the capabilities the provider must have and the tools the agent is given. The designer
reads only (its roadmap changes travel in its reply); the combined implementer, reviewer and
profiler edits its candidate and carries the bridged evaluation tool. Each role's id comes from
the search's id scheme, so the roles the search asks and the roles the host declares cannot
drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.core_roles import MEASURING_WRITER, RoleKind, core_role
from vibesys.orchestration.single.core_policy.api import prompts
from vibesys.orchestration.single.search import api as search
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import WorkspaceAccess

if TYPE_CHECKING:
    from vs_core.api.search import Role
    from vs_runtime.api import AgentRole

_RENDERER = TemplateRenderer(Path(prompts.__file__).parent)

# The designer reads the run root and writes nothing: its roadmap changes travel in its reply.
DESIGNER_KIND = RoleKind(WorkspaceAccess.READ_ONLY, frozenset())


def _role(role: Role, kind: RoleKind) -> AgentRole:
    return core_role(
        _RENDERER,
        role_id=search.single.ids.role_id(role.name).root,
        system_template=f"{role.name}_system.j2",
        kind=kind,
    )


DESIGNER = _role(search.PLANNER, DESIGNER_KIND)
IMPLEMENTER = _role(search.IMPLEMENTER, MEASURING_WRITER)

CORE_ROLES = (DESIGNER, IMPLEMENTER)
