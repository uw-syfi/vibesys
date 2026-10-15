"""An agent spec runs a vetted provider and model pair, or says why not."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import (
    HARNESSES,
    SHIPPED_PROVIDERS,
    AgentBackend,
    AgentSpec,
    Harness,
    UnvettedHarnessError,
    default_model,
    vetted_models,
)

_PROVIDERS = st.sampled_from(SHIPPED_PROVIDERS)
_MODELS = st.text(min_size=1, max_size=40)


def test_every_vetted_pair_is_a_shipped_provider_once() -> None:
    assert {harness.provider for harness in HARNESSES} <= set(SHIPPED_PROVIDERS)
    assert len(set(HARNESSES)) == len(HARNESSES)


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda harness: harness.model)
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
