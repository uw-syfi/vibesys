"""The host allow-list narrows live CLI runs and is not bypassed by resume."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.api import (
    Config,
    ConfigurationError,
    agent_spec_from_config,
    load_host_allowed_models,
    vetted_models,
)
from vibesys.composition import resolve_agent_specs
from vs_agent.api import ALLOWED_MODELS_SETTING, SHIPPED_PROVIDERS, AgentBackend
from vs_runtime.api import AgentRole

if TYPE_CHECKING:
    from pathlib import Path

_ROLES = (AgentRole(id="worker", system_prompt="work"),)
_KEYS = st.from_regex(r"[a-z][a-z0-9_]{0,16}", fullmatch=True).filter(
    lambda key: key != ALLOWED_MODELS_SETTING
)


def _provider() -> str:
    return SHIPPED_PROVIDERS[0]


def _kept() -> str:
    models = vetted_models(_provider())
    assert models
    return models[0]


def test_a_missing_or_empty_host_file_sets_no_restriction(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"
    empty = tmp_path / "host.toml"
    empty.write_text(f"{ALLOWED_MODELS_SETTING} = []\n")
    blank = tmp_path / "blank.toml"
    blank.write_text("\n")

    assert load_host_allowed_models(missing) == frozenset()
    assert load_host_allowed_models(empty) == frozenset()
    assert load_host_allowed_models(blank) == frozenset()
    spec = agent_spec_from_config(Config(), provider=_provider(), host_settings=missing)
    assert spec.allowed_models == frozenset()
    assert spec.model == _kept()


def test_the_host_file_names_the_models_it_lists(tmp_path: Path) -> None:
    path = tmp_path / "host.toml"
    path.write_text(f'{ALLOWED_MODELS_SETTING} = ["{_kept()}", "other-model"]\n')
    assert load_host_allowed_models(path) == frozenset({_kept(), "other-model"})


@given(key=_KEYS)
def test_an_unknown_key_in_the_host_setting_is_rejected_naming_the_key(
    key: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    path = tmp_path_factory.mktemp("host-settings") / "host.toml"
    path.write_text(f"{key} = true\n")
    with pytest.raises(ValueError, match=key):
        load_host_allowed_models(path)


def test_a_blank_model_id_names_the_setting_key(tmp_path: Path) -> None:
    path = tmp_path / "host.toml"
    path.write_text(f'{ALLOWED_MODELS_SETTING} = ["", "   "]\n')
    with pytest.raises(ValueError, match=ALLOWED_MODELS_SETTING):
        load_host_allowed_models(path)


def test_a_host_file_that_is_not_settings_names_the_file(tmp_path: Path) -> None:
    directory = tmp_path / "host.toml"
    directory.mkdir()
    broken = tmp_path / "broken.toml"
    broken.write_text(f"{ALLOWED_MODELS_SETTING} = [\n")
    for path in (directory, broken):
        with pytest.raises(ValueError, match=path.name):
            load_host_allowed_models(path)


def test_a_bad_host_file_refuses_before_a_run_and_names_the_key(tmp_path: Path) -> None:
    path = tmp_path / "host.toml"
    path.write_text("not_a_setting = true\n")
    with pytest.raises(ConfigurationError, match="not_a_setting"):
        agent_spec_from_config(Config(), provider=_provider(), host_settings=path)


def test_resume_does_not_bypass_the_host_allow_list(tmp_path: Path) -> None:
    """Resume sets the unvetted opt-out; the host list is still checked, and names the role."""
    kept = _kept()
    path = tmp_path / "host.toml"
    path.write_text(f'{ALLOWED_MODELS_SETTING} = ["{kept}"]\n')
    config = Config.model_validate(
        {
            "model": {"name": kept},
            "agent": {"roles": {"worker": {"model": "outside-the-set"}}},
        }
    )
    with pytest.raises(ConfigurationError) as raised:
        resolve_agent_specs(
            config,
            _ROLES,
            provider=_provider(),
            allow_unvetted_model=True,
            host_settings=path,
        )
    message = str(raised.value)
    assert ALLOWED_MODELS_SETTING in message
    assert "worker" in message
    assert "outside-the-set" in message
    assert kept in message


def test_a_vetted_model_outside_the_list_is_refused_without_the_opt_out(tmp_path: Path) -> None:
    models = vetted_models(_provider())
    if len(models) < 2:
        pytest.skip("this provider vets fewer than two models")
    kept, refused = models[0], models[1]
    path = tmp_path / "host.toml"
    path.write_text(f'{ALLOWED_MODELS_SETTING} = ["{kept}"]\n')
    config = Config.model_validate({"model": {"name": refused}})
    with pytest.raises(ConfigurationError) as raised:
        resolve_agent_specs(config, _ROLES, provider=_provider(), host_settings=path)
    message = str(raised.value)
    assert ALLOWED_MODELS_SETTING in message
    assert refused in message


def test_a_scripted_run_is_admitted_outside_the_list() -> None:
    config = Config.model_validate(
        {"model": {"name": "outside-the-set"}, "agent": {"backend": "scripted"}}
    )
    specs = resolve_agent_specs(
        config, _ROLES, allowed_models=frozenset({_kept()}), allow_unvetted_model=False
    )
    assert specs["worker"].backend is AgentBackend.SCRIPTED
    assert specs["worker"].model == "outside-the-set"


def test_a_fallback_model_outside_the_list_is_refused_on_resume() -> None:
    kept = _kept()
    fallback_provider = SHIPPED_PROVIDERS[-1]
    config = Config.model_validate(
        {
            "model": {"name": kept},
            "agent": {
                "quota": {
                    "fallback_provider": fallback_provider,
                    "fallback_model": "outside-the-set",
                }
            },
        }
    )
    with pytest.raises(ConfigurationError) as raised:
        resolve_agent_specs(
            config,
            _ROLES,
            provider=_provider(),
            allow_unvetted_model=True,
            allowed_models=frozenset({kept}),
        )
    assert "outside-the-set" in str(raised.value)
    assert ALLOWED_MODELS_SETTING in str(raised.value)
