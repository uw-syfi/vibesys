"""`[agent.roles.<id>]` model overrides address `multi`'s roles by their declared role ids.

Design question Q6 of `docs/design/strategies-on-core.md`. A role override in `agent.toml` is
keyed by a role id the selected plugin declares, and any other key is refused before the run
opens. `multi` declares `multi-orchestrator`, `multi-profiler`, `multi-implementer` and `multi-judge`, so those are the keys
(the router is the orchestrator asked another question, so it has no key of its own);
the legacy ids of the loop it replaces (`orchestrator`, `implementer`) are not declared and are
refused naming the key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.api import vetted_models
from vibesys.composition import resolve_agent_specs
from vibesys.config import Config
from vibesys.errors import ConfigurationError
from vibesys.orchestration.multi.plugin import PLUGIN, PROFILE_GUIDED_PLUGIN
from vibesys.orchestration.multi.search.api import multi
from vs_agent.api import DEFAULT_CLI_PROVIDER
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vs_runtime.api import OrchestrationPlugin

MULTI_ROLES = (
    "multi-orchestrator",
    "multi-profiler",
    "multi-implementer",
    "multi-judge",
)
LEGACY_ROLES = ("orchestrator", "profiler", "implementer", "judge")


def _config(roles: dict[str, dict[str, str]]) -> Config:
    return Config.model_validate({"model": {"name": "gpt-5.5"}, "agent": {"roles": roles}})


@pytest.mark.parametrize("plugin", [PLUGIN, PROFILE_GUIDED_PLUGIN])
def test_each_preset_declares_the_prefixed_role_ids(plugin: OrchestrationPlugin) -> None:
    assert tuple(role.id for role in plugin.agents) == MULTI_ROLES


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    overrides=st.dictionaries(
        st.sampled_from(MULTI_ROLES),
        st.sampled_from(vetted_models(DEFAULT_CLI_PROVIDER)),
    )
)
def test_an_override_changes_only_the_role_it_names(overrides: dict[str, str]) -> None:
    config = _config({role: {"model": model} for role, model in overrides.items()})
    specs = resolve_agent_specs(config, PLUGIN.agents)
    assert {role: spec.model for role, spec in specs.items()} == {
        role: overrides.get(role, "gpt-5.5") for role in MULTI_ROLES
    }


@pytest.mark.parametrize("legacy", LEGACY_ROLES)
def test_a_legacy_role_id_is_refused_naming_the_key(legacy: str) -> None:
    config = _config({legacy: {"model": "other"}})
    with pytest.raises(ConfigurationError, match=f"agent.roles.{legacy}"):
        resolve_agent_specs(config, PLUGIN.agents)


@settings(max_examples=examples(pr=8, nightly=100))
@given(model=st.text(alphabet="abcdefghij-", min_size=1, max_size=12))
def test_the_orchestrators_model_is_the_routers_too(model: str) -> None:
    chosen = multi.with_models({"orchestrator": model})

    asked = {role.name: role.model for role in chosen.roles}
    assert asked["orchestrator"] == model
    assert asked["router"] == model
    assert asked["profiler"] is None
