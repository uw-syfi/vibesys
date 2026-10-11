"""The agent roles of a single run on the core path.

They are declared here, outside the legacy orchestration tree, because a role is host wiring: its
capabilities and workspace access name `vs_runtime` values and its prompt is read from a template
file, neither of which the pure policy may do. The designer may write the roadmap artifacts of
its run-scoped turn; the combined implementer, reviewer and profiler edits its candidate and
carries the bridged evaluation tool. Each role's id is the strategy's (`role_id`), so the roles
the strategy names and the roles the host declares cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.core_roles import MEASURING_WRITER, RoleKind, core_role
from vibesys.orchestration.single.core_policy.api import prompts
from vibesys.orchestration.single.strategy.api import Role, role_id
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import WorkspaceAccess

if TYPE_CHECKING:
    from vs_runtime.api import AgentRole

_RENDERER = TemplateRenderer(Path(prompts.__file__).parent)

# The designer writes `roadmap/` in the run root and reads everything else.
DESIGNER_KIND = RoleKind(WorkspaceAccess.LIMITED, frozenset())


def _role(role: Role, kind: RoleKind) -> AgentRole:
    return core_role(
        _RENDERER,
        role_id=role_id(role.value).root,
        system_template=f"{role.value}_system.j2",
        kind=kind,
    )


DESIGNER = _role(Role.PLANNER, DESIGNER_KIND)
IMPLEMENTER = _role(Role.IMPLEMENTER, MEASURING_WRITER)

CORE_ROLES = (DESIGNER, IMPLEMENTER)
