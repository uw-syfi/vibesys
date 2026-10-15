"""`[agent.roles.<id>]` model overrides address a core strategy's roles by its declared role ids.

Design question Q6 of `docs/design/strategies-on-core.md`. A role override in `agent.toml` is
keyed by a role id the selected plugin declares, and any other key is refused before the run
opens. `dynamic` declares `dynamic-orchestrator`, `dynamic-implementer`, `dynamic-judge`,
`dynamic-profiler`, `dynamic-merger` and `dynamic-outliner`, so those are the keys; the legacy ids of the loops it replaces
(`orchestrator`, `implementer`, `judge`, `profiler`) are not declared and are refused naming the
key. The ported strategies follow the same rule with their own prefix.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.api import vetted_models
from vibesys.composition import resolve_agent_specs
from vibesys.config import Config
from vibesys.errors import ConfigurationError
from vibesys.orchestration.dynamic.plugin import PLUGIN
from vibesys.orchestration.dynamic.search.api import ROLES, dynamic
from vs_agent.api import DEFAULT_CLI_PROVIDER
from vs_core.api import UnknownRoleError
from vs_sim.api.testing import examples

DYNAMIC_ROLES = (
    "dynamic-orchestrator",
    "dynamic-implementer",
    "dynamic-judge",
    "dynamic-profiler",
    "dynamic-merger",
    "dynamic-outliner",
)
LEGACY_ROLES = ("orchestrator", "implementer", "judge", "profiler")


def _config(roles: dict[str, dict[str, str]]) -> Config:
    return Config.model_validate({"model": {"name": "gpt-5.5"}, "agent": {"roles": roles}})


def test_the_plugin_declares_the_prefixed_role_ids() -> None:
    assert tuple(role.id for role in PLUGIN.agents) == DYNAMIC_ROLES


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    overrides=st.dictionaries(
        st.sampled_from(DYNAMIC_ROLES),
        st.sampled_from(vetted_models(DEFAULT_CLI_PROVIDER)),
    )
)
def test_an_override_changes_only_the_role_it_names(overrides: dict[str, str]) -> None:
    config = _config({role: {"model": model} for role, model in overrides.items()})
    specs = resolve_agent_specs(config, PLUGIN.agents)
    assert {role: spec.model for role, spec in specs.items()} == {
        role: overrides.get(role, "gpt-5.5") for role in DYNAMIC_ROLES
    }


@pytest.mark.parametrize("legacy", LEGACY_ROLES)
def test_a_legacy_role_id_is_refused_naming_the_key(legacy: str) -> None:
    config = _config({legacy: {"model": "other"}})
    with pytest.raises(ConfigurationError, match=f"agent.roles.{legacy}"):
        resolve_agent_specs(config, PLUGIN.agents)


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
        dynamic.with_models(dict.fromkeys(unknown, "m") | {ROLE_NAMES[0]: "m"})
    assert all(name in str(raised.value) for name in unknown)
