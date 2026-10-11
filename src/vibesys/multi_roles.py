"""The agent roles of a multi run on the core path.

They are declared here, outside the legacy orchestration tree, because a role is host wiring: its
capabilities and workspace access name `vs_runtime` values and its prompt is read from a template
file, neither of which the pure policy may do. The designer may write the roadmap artifacts of its
run-scoped turns; the profiler writes bounded evidence below `profile/` and reads captures through
the stateless profiler tool; the implementer edits its candidate and, like the judge that reads
it, carries the bridged evaluation tool. Each role's id is the strategy's (`role_id`), so the roles
the strategy names and the roles the host declares cannot drift apart.
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
from vibesys.orchestration.multi.strategy.api import Role, role_id
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import AgentCapability, AgentTool, WorkspaceAccess

if TYPE_CHECKING:
    from vs_runtime.api import AgentRole

_RENDERER = TemplateRenderer(Path(prompts.__file__).parent)

# The designer writes `roadmap/` in the run root and reads everything else.
DESIGNER_KIND = RoleKind(WorkspaceAccess.LIMITED, frozenset())
# The profiler writes its evidence below `profile/` and analyses captures with the profiler's
# tool server, which holds no run state (so it is no second writer beside core).
PROFILER_KIND = RoleKind(
    WorkspaceAccess.LIMITED,
    frozenset({AgentCapability.MCP_SERVERS}),
    (AgentTool(id="profiler", stateless=True),),
)


def _role(role: Role, kind: RoleKind) -> AgentRole:
    return core_role(
        _RENDERER,
        role_id=role_id(role.value).root,
        system_template=f"{role.value}_system.j2",
        kind=kind,
    )


DESIGNER = _role(Role.PLANNER, DESIGNER_KIND)
PROFILER = _role(Role.PROFILER, PROFILER_KIND)
IMPLEMENTER = _role(Role.IMPLEMENTER, MEASURING_WRITER)
JUDGE = _role(Role.JUDGE, MEASURING_READER)

CORE_ROLES = (DESIGNER, PROFILER, IMPLEMENTER, JUDGE)
