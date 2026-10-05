"""A core session mounts what its role's own configuration declares, for every role."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from vibesys.run.core_services import agent_session_spec
from vs_agent.api import AgentSpec
from vs_agent.api.testing import FakeAgentClient
from vs_runtime.api import AgentRole
from vs_runtime.api.infrastructure import AgentExecutionConfiguration
from vs_sandbox.api import HostResource, HostResourceAccess, ProjectPathPolicy

_SHARED = HostResource(Path("/shared"), HostResourceAccess.READ_ONLY, "run-wide mount")


@dataclass(frozen=True)
class _Environment:
    """The run's one agent environment: only what the spec factory reads."""

    host_resources: tuple[HostResource, ...] = (_SHARED,)
    project_path_policy: ProjectPathPolicy = field(default_factory=ProjectPathPolicy)
    skill_source_dirs: tuple[Path, ...] = ()
    use_docker: bool = False


_role_ids = st.lists(
    st.from_regex(r"[a-z][a-z-]{0,8}", fullmatch=True), min_size=1, max_size=5, unique=True
)


@given(role_ids=_role_ids, mounted=st.sets(st.integers(min_value=0, max_value=4)))
def test_each_role_session_mounts_its_own_configured_resources(
    role_ids: list[str], mounted: set[int]
) -> None:
    """The evaluation socket is declared per role; a role without it must not get it."""
    spec = AgentSpec()
    own = {
        role_id: HostResource(
            Path(f"/run/{role_id}.sock"), HostResourceAccess.READ_WRITE, "role tool socket"
        )
        for index, role_id in enumerate(role_ids)
        if index in mounted
    }

    def configuration(role: AgentRole) -> AgentExecutionConfiguration:
        return AgentExecutionConfiguration(
            agent_id=role.id,
            spec=spec,
            resources=(own[role.id],) if role.id in own else (),
        )

    spec_for = agent_session_spec(
        client=FakeAgentClient(),
        environment=_Environment(),
        specs=dict.fromkeys(role_ids, spec),
        variables=dict,
        configuration=configuration,
    )
    for role_id in role_ids:
        session = spec_for(AgentRole(id=role_id, system_prompt="x"), Path("/workspace"))
        expected = (_SHARED, *((own[role_id],) if role_id in own else ()))
        assert session.policy.host_resources == expected
