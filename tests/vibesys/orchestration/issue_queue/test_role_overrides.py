"""`[agent.roles.<id>]` model overrides address the issue queue's roles by their declared ids.

Design question Q6 of `docs/design/strategies-on-core.md`, as pinned for `dynamic`: an override is
keyed by a role id the plugin declares (`plain-implementer`, `plain-judge`,
`plain-perf-evaluator`), and a legacy id of the loop it replaces is refused naming the key.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.api import vetted_models
from vibesys.composition import resolve_agent_specs
from vibesys.config import Config
from vibesys.errors import ConfigurationError
from vibesys.orchestration.issue_queue.plugin import PLUGIN
from vs_agent.api import DEFAULT_CLI_PROVIDER
from vs_sim.api.testing import examples

PLAIN_ROLES = ("plain-implementer", "plain-judge", "plain-perf-evaluator")
LEGACY_ROLES = ("implementer", "judge", "perf_eval")


def _config(roles: dict[str, dict[str, str]]) -> Config:
    return Config.model_validate({"model": {"name": "gpt-5.5"}, "agent": {"roles": roles}})


def test_the_plugin_declares_the_prefixed_role_ids() -> None:
    assert tuple(role.id for role in PLUGIN.agents) == PLAIN_ROLES


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    overrides=st.dictionaries(
        st.sampled_from(PLAIN_ROLES),
        st.sampled_from(vetted_models(DEFAULT_CLI_PROVIDER)),
    )
)
def test_an_override_changes_only_the_role_it_names(overrides: dict[str, str]) -> None:
    config = _config({role: {"model": model} for role, model in overrides.items()})
    specs = resolve_agent_specs(config, PLUGIN.agents)
    assert {role: spec.model for role, spec in specs.items()} == {
        role: overrides.get(role, "gpt-5.5") for role in PLAIN_ROLES
    }


@pytest.mark.parametrize("legacy", LEGACY_ROLES)
def test_a_legacy_role_id_is_refused_naming_the_key(legacy: str) -> None:
    config = _config({legacy: {"model": "other"}})
    with pytest.raises(ConfigurationError, match=f"agent.roles.{legacy}"):
        resolve_agent_specs(config, PLUGIN.agents)
