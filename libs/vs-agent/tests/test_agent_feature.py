"""The ``vibesys-agent`` Feature is well formed and installs exactly the pins in ``provider_policy``."""

from __future__ import annotations

import json
from typing import Any

from vs_agent.api import CLI_VERSIONS, CONTAINER_PYTHON_HOME, CONTAINER_PYTHON_VERSION, NODE_VERSION
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
