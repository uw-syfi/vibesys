"""An agent spec runs a vetted provider and model pair, or says why not."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import agentshim
import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import (
    ALLOWED_MODELS_SETTING,
    DEFAULT_CLI_PROVIDER,
    SHIPPED_PROVIDERS,
    AgentBackend,
    AgentSpec,
    Harness,
    ModelNotAllowedError,
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
_TOKENS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-.", min_size=1, max_size=16)
_ALLOWED = st.frozensets(_TOKENS, max_size=3)
_ROLES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12)


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


def _cli_admitted(provider: str, model: str, allowed: frozenset[str], *, unvetted: bool) -> bool:
    """A CLI model is admitted when the host allows it and it is vetted or opted out."""
    in_set = not allowed or model in allowed
    return in_set and (unvetted or model in vetted_models(provider))


@given(
    provider=_PROVIDERS,
    model=_TOKENS,
    role=_ROLES,
    allowed=_ALLOWED,
    opt_out=st.sampled_from(("yes", "no")),
)
def test_a_cli_model_is_admitted_only_when_the_host_allows_it_and_it_is_vetted(
    provider: str, model: str, role: str, allowed: frozenset[str], opt_out: str
) -> None:
    """For any allow-list and any role model, a CLI spec is admitted exactly then."""
    unvetted = opt_out == "yes"
    admitted = _cli_admitted(provider, model, allowed, unvetted=unvetted)
    try:
        spec = AgentSpec(
            provider=provider,
            model=model,
            role_models={role: model},
            allow_unvetted_model=unvetted,
            allowed_models=allowed,
        )
    except (UnvettedHarnessError, ModelNotAllowedError):
        assert not admitted
    else:
        assert admitted
        assert spec.model == model
        assert spec.role_models[role] == model


@given(provider=_PROVIDERS, model=_TOKENS, allowed=_ALLOWED)
def test_the_opt_out_does_not_bypass_the_host_allow_list(
    provider: str, model: str, allowed: frozenset[str]
) -> None:
    """``allow_unvetted_model`` (the resume opt-out) still refuses a model outside the set."""
    if not allowed or model in allowed:
        spec = AgentSpec(
            provider=provider, model=model, allow_unvetted_model=True, allowed_models=allowed
        )
        assert spec.model == model
        return
    with pytest.raises(ModelNotAllowedError) as raised:
        AgentSpec(provider=provider, model=model, allow_unvetted_model=True, allowed_models=allowed)
    assert raised.value.setting == ALLOWED_MODELS_SETTING
    assert raised.value.role is None
    assert raised.value.model == model
    assert set(raised.value.allowed) == set(allowed)


@given(provider=_PROVIDERS, model=_TOKENS, role=_ROLES, allowed=_ALLOWED)
def test_a_role_model_outside_the_set_names_the_role(
    provider: str, model: str, role: str, allowed: frozenset[str]
) -> None:
    if not allowed or model in allowed:
        return
    with pytest.raises(ModelNotAllowedError) as raised:
        AgentSpec(
            provider=provider,
            model=None,
            role_models={role: model},
            allow_unvetted_model=True,
            allowed_models=allowed,
        )
    assert raised.value.setting == ALLOWED_MODELS_SETTING
    assert raised.value.role == role
    assert raised.value.model == model
    assert set(raised.value.allowed) == set(allowed)


@pytest.mark.parametrize("backend", [item for item in AgentBackend if item is not AgentBackend.CLI])
@given(provider=_PROVIDERS, model=_TOKENS, allowed=_ALLOWED)
def test_scripted_and_other_non_cli_backends_are_admitted(
    backend: AgentBackend, provider: str, model: str, allowed: frozenset[str]
) -> None:
    """Scripted runs, and the stub backend, call no live model, so the allow-list does not apply."""
    spec = AgentSpec(backend=backend, provider=provider, model=model, allowed_models=allowed)
    assert spec.model == model
    assert spec.backend is backend


def test_a_model_swapped_in_later_is_held_to_the_allow_list() -> None:
    """A quota fallback or role override goes through ``replace``, which checks again."""
    provider = SHIPPED_PROVIDERS[0]
    kept = vetted_models(provider)[0]
    spec = AgentSpec(provider=provider, model=kept, allowed_models=frozenset({kept}))
    with pytest.raises(ModelNotAllowedError) as raised:
        replace(spec, model=f"{kept}-other", allow_unvetted_model=True)
    assert raised.value.setting == ALLOWED_MODELS_SETTING
    assert raised.value.model == f"{kept}-other"
