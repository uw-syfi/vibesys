"""A run's agent model follows its provider, and only vetted pairs resolve."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.api import (
    Config,
    ConfigurationError,
    agent_spec_from_config,
    harnesses,
    vetted_models,
)
from vibesys.composition import resolve_agent_specs
from vs_agent.api import SHIPPED_PROVIDERS
from vs_runtime.api import AgentRole

_ROLES = (AgentRole(id="worker", system_prompt="work"),)
_VETTED_PROVIDERS = st.sampled_from(sorted({harness.provider for harness in harnesses()}))
_MODELS = st.text(min_size=1, max_size=40).filter(str.strip)


@given(provider=_VETTED_PROVIDERS)
def test_a_provider_without_a_configured_model_runs_its_own_default(provider: str) -> None:
    spec = agent_spec_from_config(Config(), provider=provider)
    assert spec.model == vetted_models(provider)[0]


def test_claude_without_a_config_does_not_run_a_codex_model() -> None:
    model = agent_spec_from_config(Config(), provider="claude").model
    assert model == vetted_models("claude")[0]
    assert model not in vetted_models("codex")


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ({"model": {"name": "{model}"}}, "model.name"),
        ({"agent": {"roles": {"worker": {"model": "{model}"}}}}, "agent.roles.worker.model"),
        (
            {"agent": {"quota": {"fallback_provider": "codex", "fallback_model": "{model}"}}},
            "agent.quota.fallback_model",
        ),
    ],
    ids=["model", "role", "fallback"],
)
@given(model=_MODELS)
def test_an_unvetted_model_names_the_key_that_chose_it(
    raw: dict[str, object], key: str, model: str
) -> None:
    if model in vetted_models("claude") or model in vetted_models("codex"):
        return
    config = Config.model_validate(_fill(raw, model))
    with pytest.raises(ConfigurationError) as raised:
        resolve_agent_specs(config, _ROLES, provider="claude")
    assert raised.value.diagnostic.code == "agent_harness_unvetted"
    assert raised.value.diagnostic.message.startswith(f"{key}")
    specs = resolve_agent_specs(config, _ROLES, provider="claude", allow_unvetted_model=True)
    assert set(specs) == {"worker"}


@pytest.mark.parametrize(
    "provider", [provider for provider in SHIPPED_PROVIDERS if not vetted_models(provider)]
)
def test_a_provider_with_no_vetted_model_needs_one_named(provider: str) -> None:
    with pytest.raises(ConfigurationError, match="no model is named"):
        resolve_agent_specs(Config(), _ROLES, provider=provider)
    specs = resolve_agent_specs(
        Config.model_validate({"model": {"name": "any"}}),
        _ROLES,
        provider=provider,
        allow_unvetted_model=True,
    )
    assert specs["worker"].model == "any"


def _fill(raw: object, model: str) -> object:
    if isinstance(raw, dict):
        return {key: _fill(value, model) for key, value in raw.items()}
    return model if raw == "{model}" else raw
