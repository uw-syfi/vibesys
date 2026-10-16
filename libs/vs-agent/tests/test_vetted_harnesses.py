"""An agent spec runs a vetted provider and model pair, or says why not."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import agentshim
import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import (
    DEFAULT_CLI_PROVIDER,
    SHIPPED_PROVIDERS,
    AgentBackend,
    AgentSpec,
    Harness,
    UnvettedHarnessError,
    default_model,
    harnesses,
    vetted_models,
)
from vs_agent.api.testing import fake_profiles

if TYPE_CHECKING:
    from collections.abc import Mapping

_PROVIDERS = st.sampled_from(SHIPPED_PROVIDERS)
_MODELS = st.text(min_size=1, max_size=40)


def test_every_vetted_pair_is_a_shipped_provider_once() -> None:
    assert {harness.provider for harness in harnesses()} <= set(SHIPPED_PROVIDERS)
    assert len(set(harnesses())) == len(harnesses())


@pytest.mark.parametrize("harness", harnesses(), ids=lambda harness: harness.model)
def test_every_vetted_pair_builds_a_spec(harness: Harness) -> None:
    spec = AgentSpec(provider=harness.provider, model=harness.model)
    assert (spec.provider, spec.model) == (harness.provider, harness.model)


@given(provider=_PROVIDERS)
def test_a_provider_default_is_its_first_vetted_model(provider: str) -> None:
    models = vetted_models(provider)
    assert default_model(provider) == (models[0] if models else None)


@given(provider=_PROVIDERS, model=_MODELS)
def test_an_unvetted_model_is_rejected_naming_the_vetted_ones(provider: str, model: str) -> None:
    if model in vetted_models(provider):
        return
    with pytest.raises(UnvettedHarnessError) as raised:
        AgentSpec(provider=provider, model=model)
    assert raised.value.allowed == vetted_models(provider)
    assert repr(model) in str(raised.value)
    with pytest.raises(UnvettedHarnessError):
        AgentSpec(provider=provider, role_models={"worker": model})


@given(provider=_PROVIDERS, model=_MODELS)
def test_the_opt_out_and_the_stub_backend_run_any_model(provider: str, model: str) -> None:
    assert AgentSpec(provider=provider, model=model, allow_unvetted_model=True).model == model
    assert AgentSpec(backend=AgentBackend.STUB, provider=provider, model=model).model == model


def test_a_swapped_in_model_is_checked_again() -> None:
    spec = AgentSpec(provider="claude", model="claude-opus-5")
    with pytest.raises(UnvettedHarnessError):
        replace(spec, model="gpt-5.4")


_MODEL_LISTS = st.lists(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789.-/", min_size=1, max_size=12),
    unique=True,
    max_size=3,
).map(tuple)


def _install(monkeypatch: pytest.MonkeyPatch, models: Mapping[str, tuple[str, ...]]) -> None:
    fake_profiles.install(
        monkeypatch,
        {name: fake_profiles.profile(name, models=listed) for name, listed in models.items()},
    )


@given(
    lists=st.lists(_MODEL_LISTS, min_size=len(SHIPPED_PROVIDERS), max_size=len(SHIPPED_PROVIDERS))
)
def test_the_vetted_pairs_are_agentshims_models_of_each_shipped_provider(
    lists: list[tuple[str, ...]],
) -> None:
    """agentshim owns which models a provider runs and its default; VibeSys only orders providers."""
    models = dict(zip(SHIPPED_PROVIDERS, lists, strict=True))
    with pytest.MonkeyPatch.context() as monkeypatch:
        _install(monkeypatch, models)
        offered = harnesses()
        order = [DEFAULT_CLI_PROVIDER, *(p for p in SHIPPED_PROVIDERS if p != DEFAULT_CLI_PROVIDER)]
        assert offered == tuple(
            Harness(provider, model) for provider in order for model in models[provider]
        )
        for provider, listed in models.items():
            assert vetted_models(provider) == listed
            assert default_model(provider) == (listed[0] if listed else None)


def test_a_provider_vibesys_does_not_ship_has_no_vetted_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, {"copilot": ("some-model",)})
    assert vetted_models("copilot") == ()
    assert default_model("copilot") is None


@pytest.mark.parametrize("provider", SHIPPED_PROVIDERS)
def test_the_shipped_vetted_models_are_the_libraries(provider: str) -> None:
    assert vetted_models(provider) == agentshim.get_provider(provider).profile.models


def test_the_current_claude_models_are_vetted() -> None:
    """Regression: a hand-kept list refused ``claude-opus-5-5`` without the opt-out."""
    for model in ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
        assert AgentSpec(provider="claude", model=model).model == model


def test_codex_vets_the_gpt_6_families_its_cli_lists_and_keeps_its_default() -> None:
    """The ids ``codex debug models`` (codex-cli 0.160.0) lists, the default still first."""
    models = vetted_models("codex")

    for model in ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna"):
        assert model in models
        assert AgentSpec(provider="codex", model=model).model == model
    assert default_model("codex") == "gpt-5.6-sol"
