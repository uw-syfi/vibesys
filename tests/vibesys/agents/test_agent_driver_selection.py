"""Tests for selecting an agent driver through application configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from vibesys.agents import build_agent_client
from vibesys.agents.client import AgentClient
from vibesys.agents.drivers.agentshim import AgentShimDriver
from vibesys.agents.drivers.mock import MockDriver
from vibesys.agents.drivers.omnigent import OmnigentDriver, OmnigentDriverError
from vibesys.agents.factory import agent_driver_supports_mcp_servers, supported_cli_providers
from vibesys.agents.omnigent import supported_providers
from vibesys.agents.omnigent.providers import OMNIGENT_PROVIDER_EXECUTORS
from vibesys.config import Config
from vs_sandbox import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def _config(**agent: object) -> Config:
    return Config.model_validate({"model": {"name": "m"}, "agent": agent})


def _build(  # noqa: PLR0913
    config: Config,
    *,
    backends: dict[str, Any] | None = None,
    model_name: str = "m",
    use_docker: bool = False,
    log_dir: Path | None = None,
    host_resources: Iterable[HostResource] = (),
) -> AgentClient:
    client = build_agent_client(
        config,
        agent_backend=None,
        cli_provider=None,
        backends=backends,
        skills=[],
        skill_source_dirs=[],
        model=None,
        model_name=model_name,
        run_log_file=None,
        use_docker=use_docker,
        log_dir=log_dir,
        host_resources=host_resources,
    )
    # Every case here selects the cli backend, whose concrete client is what
    # these tests inspect.
    assert isinstance(client, AgentClient)
    return client


def test_agentshim_is_the_default_driver() -> None:
    client = _build(_config(backend="cli", cli_provider="codex"))

    assert isinstance(client._driver, AgentShimDriver)  # noqa: SLF001


@pytest.mark.parametrize("provider", ["claude", "gemini", "codex", "opencode"])
def test_default_driver_supports_all_agentshim_providers(provider: str) -> None:
    client = _build(_config(backend="cli", cli_provider=provider))

    assert isinstance(client._driver, AgentShimDriver)  # noqa: SLF001
    assert client._provider == provider  # noqa: SLF001


def test_agentshim_docker_configuration_is_preserved() -> None:
    backends = {"implementer": MagicMock(), "judge": MagicMock(), "perf_eval": MagicMock()}

    client = _build(
        _config(backend="cli", cli_provider="claude"),
        backends=backends,
        use_docker=True,
    )

    assert isinstance(client._driver, AgentShimDriver)  # noqa: SLF001
    assert client._driver._docker_sandboxes is backends  # noqa: SLF001


def test_omnigent_driver_can_be_selected() -> None:
    client = _build(_config(driver="omnigent", backend="cli", cli_provider="claude"))

    assert isinstance(client._driver, OmnigentDriver)  # noqa: SLF001


def test_mock_driver_can_be_selected_as_test_infrastructure() -> None:
    client = _build(_config(driver="mock", backend="cli"))

    assert isinstance(client._driver, MockDriver)  # noqa: SLF001
    # The mock drives no CLI, so the configured provider does not apply.
    assert client.provider == "mock"
    assert supported_cli_providers("mock") == ("mock",)


def test_mock_driver_ignores_a_configured_cli_provider() -> None:
    client = _build(_config(driver="mock", backend="cli", cli_provider="codex"))

    assert isinstance(client._driver, MockDriver)  # noqa: SLF001
    assert client.provider == "mock"


def test_unknown_driver_names_the_selectable_drivers() -> None:
    with pytest.raises(ValueError, match="mock") as exc:
        supported_cli_providers("nonesuch")

    assert "nonesuch" in str(exc.value)


@pytest.mark.parametrize(
    ("driver", "supports_mcp"),
    [(None, True), ("agentshim", True), ("omnigent", True), ("mock", True)],
)
def test_preflight_capabilities_match_constructed_driver(
    driver: str | None,
    supports_mcp: object,
) -> None:
    config = _config(driver=driver, backend="cli", cli_provider="codex")
    declared = agent_driver_supports_mcp_servers(config, agent_backend=None)
    client = _build(config)

    assert declared is supports_mcp
    assert declared is client.capabilities.mcp_servers


def test_non_cli_backend_has_no_external_driver_capabilities() -> None:
    config = _config(backend="deepagents")

    assert agent_driver_supports_mcp_servers(config, agent_backend=None) is None


def test_omnigent_selection_passes_model_and_log_dir(tmp_path) -> None:  # noqa: ANN001
    client = _build(
        _config(driver="omnigent", backend="cli", cli_provider="codex"),
        model_name="gpt-5",
        log_dir=tmp_path,
    )

    assert client._model_name == "gpt-5"  # noqa: SLF001
    assert client._log_dir == tmp_path  # noqa: SLF001


def test_driver_is_rejected_for_non_cli_backend() -> None:
    with pytest.raises(SystemExit, match="valid only"):
        _build(_config(driver="omnigent", backend="deepagents"), backends={})


@pytest.mark.parametrize("provider", ["gemini", "opencode"])
def test_omnigent_rejects_unsupported_provider_with_remedy(provider: str) -> None:
    with pytest.raises(OmnigentDriverError) as exc:
        _build(_config(driver="omnigent", backend="cli", cli_provider=provider))

    message = str(exc.value)
    assert provider in message
    assert "claude" in message
    assert "codex" in message
    assert "agentshim" in message


def test_omnigent_rejects_docker() -> None:
    with pytest.raises(SystemExit, match="--docker"):
        _build(
            _config(driver="omnigent", backend="cli", cli_provider="claude"),
            backends={"implementer": MagicMock()},
            use_docker=True,
        )


def test_omnigent_rejects_host_resource_grants(tmp_path) -> None:  # noqa: ANN001
    grant = HostResource(tmp_path / "models", HostResourceAccess.READ_ONLY, "weights")

    with pytest.raises(OmnigentDriverError) as exc:
        _build(
            _config(driver="omnigent", backend="cli", cli_provider="claude"),
            host_resources=[grant],
        )

    message = str(exc.value)
    assert "models" in message
    assert "agentshim" in message


def test_omnigent_accepts_empty_host_resources() -> None:
    client = _build(
        _config(driver="omnigent", backend="cli", cli_provider="claude"),
        host_resources=(),
    )

    assert isinstance(client._driver, OmnigentDriver)  # noqa: SLF001


def test_omnigent_provider_registry_matches_supported_providers() -> None:
    assert supported_providers() == ["claude", "codex"]
    assert set(OMNIGENT_PROVIDER_EXECUTORS) == set(supported_providers())


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_omnigent_specs_identify_inner_executors(provider: str) -> None:
    spec = OMNIGENT_PROVIDER_EXECUTORS[provider]

    assert spec.module.startswith("omnigent.inner.")
    assert spec.class_name.endswith("Executor")
    assert spec.harness
