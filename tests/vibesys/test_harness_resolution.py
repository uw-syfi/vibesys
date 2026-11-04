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


_ROLE_IDS = ("planner", "implementer", "judge")
_ROLE_CATALOGUE = tuple(AgentRole(id=role, system_prompt="work") for role in _ROLE_IDS)


@st.composite
def _role_overrides(draw: st.DrawFn) -> dict[str, dict[str, str]]:
    """Per role, none or some of a vetted provider and a model that provider vets."""
    overrides: dict[str, dict[str, str]] = {}
    for role in _ROLE_IDS:
        entry: dict[str, str] = {}
        if draw(st.booleans()):
            entry["provider"] = draw(_VETTED_PROVIDERS)
        provider = entry.get("provider")
        if draw(st.booleans()):
            pool = (
                vetted_models(provider)
                if provider is not None
                else tuple({m for h in harnesses() for m in vetted_models(h.provider)})
            )
            entry["model"] = draw(st.sampled_from(sorted(pool)))
        if entry:
            overrides[role] = entry
    return overrides


@given(run_provider=_VETTED_PROVIDERS, overrides=_role_overrides())
def test_each_role_runs_its_own_provider_and_a_model_of_that_provider(
    run_provider: str, overrides: dict[str, dict[str, str]]
) -> None:
    """A role without a provider runs the run's; a role's model is its own or its provider's default."""
    config = Config.model_validate({"agent": {"roles": overrides}})
    unvetted = [
        role
        for role in _ROLE_IDS
        if "model" in overrides.get(role, {})
        and overrides[role]["model"]
        not in vetted_models(overrides[role].get("provider", run_provider))
    ]
    if unvetted:
        # A model one provider vets and the role's (maybe inherited) provider does not.
        with pytest.raises(ConfigurationError) as raised:
            resolve_agent_specs(config, _ROLE_CATALOGUE, provider=run_provider)
        assert raised.value.diagnostic.code == "agent_harness_unvetted"
        assert raised.value.diagnostic.message.startswith(f"agent.roles.{unvetted[0]}.model")
        return
    specs = resolve_agent_specs(config, _ROLE_CATALOGUE, provider=run_provider)
    run_model = vetted_models(run_provider)[0]
    for role in _ROLE_IDS:
        entry = overrides.get(role, {})
        provider = entry.get("provider", run_provider)
        assert specs[role].provider == provider
        expected = entry.get(
            "model", run_model if provider == run_provider else vetted_models(provider)[0]
        )
        assert specs[role].model == expected
        assert specs[role].model in vetted_models(provider)


@given(model=st.sampled_from(sorted({m for h in harnesses() for m in vetted_models(h.provider)})))
def test_a_role_model_its_provider_does_not_vet_names_the_role_and_the_provider(model: str) -> None:
    other = next(p for p in ("codex", "claude") if model not in vetted_models(p))
    config = Config.model_validate(
        {"agent": {"roles": {"planner": {"provider": other, "model": model}}}}
    )

    with pytest.raises(ConfigurationError) as raised:
        resolve_agent_specs(config, _ROLE_CATALOGUE, provider="claude")

    assert raised.value.diagnostic.code == "agent_harness_unvetted"
    assert raised.value.diagnostic.message.startswith("agent.roles.planner.model")


@given(provider=st.text(min_size=1, max_size=12).filter(lambda p: p not in SHIPPED_PROVIDERS))
def test_a_role_provider_the_product_does_not_ship_is_refused_naming_the_key(
    provider: str,
) -> None:
    config = Config.model_validate({"agent": {"roles": {"planner": {"provider": provider}}}})

    with pytest.raises(ConfigurationError) as raised:
        resolve_agent_specs(config, _ROLE_CATALOGUE, provider="claude")

    assert "agent.roles.planner.provider" in raised.value.diagnostic.message
