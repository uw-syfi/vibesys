"""The ``vibesys-agent`` Feature is well formed and installs exactly the pins in ``provider_policy``."""

from __future__ import annotations

import json
from typing import Any

import agentshim

from vs_agent.api import (
    CLI_VERSIONS,
    CONTAINER_PYTHON_HOME,
    CONTAINER_PYTHON_VERSION,
    NODE_VERSION,
    SHIPPED_PROVIDERS,
)
from vs_agent.api.images import AGENT_FEATURES_PROJECT, AGENT_USER, agent_feature
from vs_sandbox.api import feature_project_problems


def _declared() -> dict[str, Any]:
    path = AGENT_FEATURES_PROJECT / "src" / "vibesys-agent" / "devcontainer-feature.json"
    return json.loads(path.read_text())["options"]


def test_the_agent_project_is_well_formed() -> None:
    assert feature_project_problems(AGENT_FEATURES_PROJECT) == []


def test_option_defaults_mirror_provider_policy() -> None:
    """The one source of each pin is ``provider_policy``; Feature defaults must equal it."""
    defaults = {name: spec["default"] for name, spec in _declared().items()}
    expected = {
        "node_version": NODE_VERSION,
        "python_version": CONTAINER_PYTHON_VERSION,
        "python_home": CONTAINER_PYTHON_HOME,
        **{f"{p}_version": v for p, v in CLI_VERSIONS.items()},
    }
    assert defaults == expected


def test_the_selected_feature_passes_every_pin_explicitly() -> None:
    feature = agent_feature()

    assert feature.feature_id == "vibesys-agent"
    assert dict(feature.options) == {name: spec["default"] for name, spec in _declared().items()}


def test_the_agent_user_is_the_one_the_feature_creates() -> None:
    script = (AGENT_FEATURES_PROJECT / "src" / "vibesys-agent" / "install.sh").read_text()
    assert f"useradd --create-home --uid 1000 --shell /bin/bash {AGENT_USER}" in script


def test_the_feature_requires_and_smoke_tests_every_shipped_cli() -> None:
    """``install.sh`` requires each shipped CLI's version and ``test.sh`` runs each binary."""
    feature = AGENT_FEATURES_PROJECT / "src" / "vibesys-agent" / "install.sh"
    smoke = AGENT_FEATURES_PROJECT / "test" / "vibesys-agent" / "test.sh"
    install_script, smoke_script = feature.read_text(), smoke.read_text()
    for provider in SHIPPED_PROVIDERS:
        binary = agentshim.get_provider(provider).profile.binary
        assert f"require_option {provider}_version" in install_script, provider
        assert f"{binary} --version" in smoke_script, provider
