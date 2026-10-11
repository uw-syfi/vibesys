"""The agent roles of an evolve run on the core path.

They are declared here, outside the legacy orchestration tree, because a role is host wiring: its
capabilities and workspace access name `vs_runtime` values and its prompt is read from a template
file, neither of which the pure policy may do. The mutator edits its candidate and carries the bridged evaluation tool, while the judge only
reads it (the framework's gates measure after its verdict, and the run admits no second
submission for the candidate the mutator already measured); the profiler only reads the candidate and
analyses captures with the stateless profiler tool. Each role's id is the strategy's (`role_id`), so
the roles the strategy names and the roles the host declares cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.core_roles import (
    CONTINUING_READER,
    MEASURING_WRITER,
    RoleKind,
    core_role,
)
from vibesys.orchestration.evolve.core_policy.api import prompts
from vibesys.orchestration.evolve.strategy.api import Role, role_id
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import AgentCapability, AgentTool, WorkspaceAccess

if TYPE_CHECKING:
    from vs_runtime.api import AgentRole

_RENDERER = TemplateRenderer(Path(prompts.__file__).parent)

# The profiler analyses captures with the profiler's tool server, which holds no run state (so it
# is no second writer beside core).
PROFILER_KIND = RoleKind(
    WorkspaceAccess.READ_ONLY,
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


MUTATOR = _role(Role.MUTATOR, MEASURING_WRITER)
JUDGE = _role(Role.JUDGE, CONTINUING_READER)
PROFILER = _role(Role.PROFILER, PROFILER_KIND)

CORE_ROLES = (MUTATOR, JUDGE, PROFILER)
