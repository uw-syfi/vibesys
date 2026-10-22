"""A core session mounts what its role's own configuration declares, for every role."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.run.core_services import agent_session_spec
from vs_agent.api import AgentBackend, AgentSpec, SkillSelection
from vs_runtime.api import AgentRole
from vs_runtime.api.infrastructure import AgentExecutionConfiguration
from vs_sandbox.api import HostResource, HostResourceAccess, ProjectPathPolicy
from vs_sim.api.testing import examples

_SHARED = HostResource(Path("/shared"), HostResourceAccess.READ_ONLY, "run-wide mount")


def _skip_nothing(_directory: str, _names: list[str]) -> set[str]:
    return set()


@dataclass(frozen=True)
class _Environment:
    """One provider's agent environment: only what the spec factory reads."""

    host_resources: tuple[HostResource, ...] = (_SHARED,)
    project_path_policy: ProjectPathPolicy = field(default_factory=ProjectPathPolicy)
    skill_source_dirs: tuple[Path, ...] = ()
    skill_selection: SkillSelection = field(
        default_factory=lambda: SkillSelection(skip_dir=_skip_nothing)
    )
    backends: None = None
    use_docker: bool = False

    def close(self) -> None:
        """Nothing is held."""


_role_ids = st.lists(
    st.from_regex(r"[a-z][a-z-]{0,8}", fullmatch=True), min_size=1, max_size=5, unique=True
)


@settings(max_examples=examples(pr=8, nightly=100))
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
        environments={spec.provider: _Environment()},
        specs=dict.fromkeys(role_ids, spec),
        variables=dict,
        configuration=configuration,
    )
    for role_id in role_ids:
        session = spec_for(AgentRole(id=role_id, system_prompt="x"), Path("/workspace"))
        expected = (_SHARED, *((own[role_id],) if role_id in own else ()))
        assert session.policy.host_resources == expected


_PROVIDERS = st.sampled_from(("codex", "claude"))


@settings(max_examples=examples(pr=8, nightly=100))
@given(providers=st.lists(_PROVIDERS, min_size=1, max_size=5), data=st.data())
def test_each_role_session_runs_its_own_provider_and_model_in_its_providers_environment(
    providers: list[str], data: st.DataObject
) -> None:
    """A role's session names the provider and model of its spec, and mounts that provider's."""
    role_ids = [f"role{index}" for index in range(len(providers))]
    models = [data.draw(st.sampled_from(("m1", "m2", "m3"))) for _ in providers]
    specs = {
        role_id: AgentSpec(provider=provider, model=model, allow_unvetted_model=True)
        for role_id, provider, model in zip(role_ids, providers, models, strict=True)
    }
    environments = {
        provider: _Environment(
            host_resources=(
                HostResource(Path(f"/{provider}"), HostResourceAccess.READ_ONLY, "login"),
            )
        )
        for provider in providers
    }
    spec_for = agent_session_spec(
        environments=environments,
        specs=specs,
        variables=dict,
        configuration=lambda role: AgentExecutionConfiguration(
            agent_id=role.id, spec=specs[role.id]
        ),
    )
    for role_id, provider, model in zip(role_ids, providers, models, strict=True):
        session = spec_for(AgentRole(id=role_id, system_prompt="x"), Path("/workspace"))
        assert (session.provider, session.model) == (provider, model)
        assert session.scripted is False
        assert session.policy.host_resources == environments[provider].host_resources


def test_a_scripted_role_keeps_its_selected_provider_and_model() -> None:
    """The executor is a separate fact. A scripted client does not erase the selection."""
    spec = AgentSpec(
        backend=AgentBackend.SCRIPTED,
        provider="claude",
        model="claude-sonnet",
        allow_unvetted_model=True,
    )
    spec_for = agent_session_spec(
        environments={spec.provider: _Environment()},
        specs={"implementer": spec},
        variables=dict,
        configuration=lambda role: AgentExecutionConfiguration(agent_id=role.id, spec=spec),
    )
    session = spec_for(AgentRole(id="implementer", system_prompt="x"), Path("/workspace"))
    assert (session.provider, session.model) == ("claude", "claude-sonnet")
    assert session.scripted is True
