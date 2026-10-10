"""Tests for selecting an agent driver through application configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, TypedDict, Unpack
from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.api import agent_spec_from_config
from vibesys.config import Config
from vs_agent.api import (
    SHIPPED_PROVIDERS,
    AgentClient,
    AgentSpec,
    ProviderNotReadyError,
    agent_supports_tool_servers,
    build_agent_client,
)
from vs_sandbox.api import SANDBOX_DISABLE_ENV

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from vs_sandbox.api import HostResource


class _BuildOptions(TypedDict, total=False):
    backends: dict[str, Any] | None
    model_name: str
    use_docker: bool
    log_dir: Path | None
    host_resources: Iterable[HostResource]


def _config(**agent: object) -> Config:
    return Config.model_validate({"model": {"name": "m"}, "agent": agent})


def _build(
    config: Config,
    **options: Unpack[_BuildOptions],
) -> AgentClient:
    spec = agent_spec_from_config(config, model=options.get("model_name", "m"))
    client = build_agent_client(
        spec=spec,
        backends=options.get("backends"),
        skill_source_dirs=[],
        run_log_file=None,
        use_docker=options.get("use_docker", False),
        log_dir=options.get("log_dir"),
        host_resources=options.get("host_resources", ()),
    )
    # Every case here selects the cli backend, whose concrete client is what
    # these tests inspect.
    assert isinstance(client, AgentClient)
    return client


def test_agentshim_is_the_default_driver() -> None:
    client = _build(_config(backend="cli", cli_provider="codex"))

    assert client.provider == "codex"


@pytest.mark.parametrize("provider", ["claude", "gemini", "codex", "opencode"])
def test_default_driver_supports_all_agentshim_providers(provider: str) -> None:
    client = _build(_config(backend="cli", cli_provider=provider))

    assert client.provider == provider


def test_agentshim_docker_configuration_is_preserved() -> None:
    backends = {"implementer": MagicMock(), "judge": MagicMock(), "perf_eval": MagicMock()}

    client = _build(
        _config(backend="cli", cli_provider="claude"),
        backends=backends,
        use_docker=True,
    )

    assert client.capabilities.container_execution
    assert not client.capabilities.host_path_grants


def test_preflight_capabilities_match_constructed_driver() -> None:
    config = _config(backend="cli", cli_provider="codex")
    spec = agent_spec_from_config(config)
    declared = agent_supports_tool_servers(spec)
    client = _build(config)

    assert declared is True
    assert declared is client.capabilities.tool_servers


def test_non_cli_backend_has_no_external_driver_capabilities() -> None:
    config = _config(backend="stub")
    spec = agent_spec_from_config(config)

    assert agent_supports_tool_servers(spec) is None


def test_agentshim_client_passes_model_and_log_dir(tmp_path: Path) -> None:
    client = _build(
        _config(backend="cli", cli_provider="codex"),
        model_name="gpt-5",
        log_dir=tmp_path,
    )

    assert client.model_for_kind("implementer") == "gpt-5"
    # A rejected attempt exercises factory logging without starting a provider CLI.
    client.close()
    with pytest.raises(RuntimeError, match="agent client is closed"):
        client.invoke_text(
            kind="implementer",
            workspace=tmp_path,
            system_prompt="instructions",
            user_prompt="prompt",
            round_label="usage log directory",
        )
    usage_record = json.loads((tmp_path / "usage.jsonl").read_text(encoding="utf-8"))
    assert usage_record["model"] == "gpt-5"
    assert usage_record["input_tokens"] is None


@given(provider=st.text(min_size=1).filter(lambda value: value not in SHIPPED_PROVIDERS))
def test_spec_rejects_any_provider_agentshim_does_not_ship(provider: str) -> None:
    """An ``AgentSpec`` rejects an unsupported provider before a client is built."""
    with pytest.raises(ValueError, match="not supported") as exc:
        AgentSpec(provider=provider)

    for supported in SHIPPED_PROVIDERS:
        assert supported in str(exc.value)


def test_agent_env_passthrough_reaches_the_spec_and_bad_names_are_rejected_at_load() -> None:
    config = _config(env_passthrough=["HF_TOKEN"])
    assert agent_spec_from_config(config).env_passthrough == ("HF_TOKEN",)

    with pytest.raises(ValueError, match=r"agent\.env_passthrough.*'HF-TOKEN'"):
        _config(env_passthrough=["HF-TOKEN"])


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_built_client_reports_a_missing_provider_cli_before_its_first_turn(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composed client probes readiness by default.

    The run's PATH holds no provider CLI, so the probe (not a turn) must fail
    with the typed error that names the provider.
    """
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(SANDBOX_DISABLE_ENV, "off")
    client = _build(_config(backend="cli", cli_provider=provider))

    with pytest.raises(ProviderNotReadyError) as raised:
        client.invoke_text(
            kind="implementer",
            workspace=tmp_path,
            system_prompt="sys",
            user_prompt="go",
            round_label="r1",
        )

    assert provider in str(raised.value)


def test_a_container_client_accepts_a_workspace_sandbox_lookup() -> None:
    """The public builder passes the lookup a core run gives its client (#1552)."""
    spec = agent_spec_from_config(_config(backend="cli", cli_provider="claude"), model="m")

    client = build_agent_client(
        spec=spec,
        backends={},
        workspace_sandboxes=lambda _path: None,
        skill_source_dirs=[],
        run_log_file=None,
        use_docker=True,
    )

    assert isinstance(client, AgentClient)
