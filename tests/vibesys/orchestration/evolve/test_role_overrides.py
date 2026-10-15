"""`[agent.roles.<id>]` model overrides address `evolve`'s roles by their declared role ids.

Design question Q6 of `docs/design/strategies-on-core.md`. `evolve` declares `evolve-mutator`,
`evolve-judge` and `evolve-profiler`; the legacy ids of the loop it replaces (`implementer`,
`judge`, `profiler`) are not declared and are refused naming the key.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.api import vetted_models
from vibesys.composition import resolve_agent_specs
from vibesys.config import Config
from vibesys.errors import ConfigurationError
from vibesys.evolve_roles import JUDGE, MUTATOR
from vibesys.orchestration.evolve.plugin import PLUGIN
from vibesys.orchestration.evolve.search.api import ROLES, evolve
from vs_agent.api import DEFAULT_CLI_PROVIDER
from vs_core.api import UnknownRoleError
from vs_sim.api.testing import examples

EVOLVE_ROLES = ("evolve-mutator", "evolve-judge", "evolve-profiler")
LEGACY_ROLES = ("implementer", "judge", "profiler")


def _config(roles: dict[str, dict[str, str]]) -> Config:
    return Config.model_validate({"model": {"name": "gpt-5.5"}, "agent": {"roles": roles}})


def test_the_plugin_declares_the_prefixed_role_ids() -> None:
    assert tuple(role.id for role in PLUGIN.agents) == EVOLVE_ROLES


def test_the_profiler_names_a_stateless_tool() -> None:
    profiler = next(role for role in PLUGIN.agents if role.id == "evolve-profiler")
    assert [(tool.id, tool.stateless) for tool in profiler.extra_tools] == [("profiler", True)]


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    overrides=st.dictionaries(
        st.sampled_from(EVOLVE_ROLES),
        st.sampled_from(vetted_models(DEFAULT_CLI_PROVIDER)),
    )
)
def test_an_override_changes_only_the_role_it_names(overrides: dict[str, str]) -> None:
    config = _config({role: {"model": model} for role, model in overrides.items()})
    specs = resolve_agent_specs(config, PLUGIN.agents)
    assert {role: spec.model for role, spec in specs.items()} == {
        role: overrides.get(role, "gpt-5.5") for role in EVOLVE_ROLES
    }


@pytest.mark.parametrize("legacy", LEGACY_ROLES)
def test_a_legacy_role_id_is_refused_naming_the_key(legacy: str) -> None:
    config = _config({legacy: {"model": "other"}})
    with pytest.raises(ConfigurationError, match=f"agent.roles.{legacy}"):
        resolve_agent_specs(config, PLUGIN.agents)


def test_the_judge_has_the_evaluation_tool_the_mutator_has() -> None:
    """The judge reads the mutator's evaluation through the same bridged tool."""
    assert [tool.id for tool in JUDGE.extra_tools] == [tool.id for tool in MUTATOR.extra_tools]


ROLE_NAMES = tuple(role.name for role in ROLES)


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    unknown=st.lists(
        st.text(alphabet="abcdefghij-", min_size=1, max_size=12).filter(
            lambda name: name not in ROLE_NAMES
        ),
        min_size=1,
        unique=True,
    )
)
def test_a_model_for_a_role_the_search_does_not_declare_is_rejected_and_named(
    unknown: list[str],
) -> None:
    with pytest.raises(UnknownRoleError) as raised:
        evolve.with_models(dict.fromkeys(unknown, "m") | {ROLE_NAMES[0]: "m"})
    assert all(name in str(raised.value) for name in unknown)
