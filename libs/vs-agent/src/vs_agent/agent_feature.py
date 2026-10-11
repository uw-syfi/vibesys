"""The ``vibesys-agent`` Dev Container Feature: how this library describes its image layer.

The Feature installs Node, the shipped provider CLIs, uv, VibeSys's Python with
the MCP library, and the non-root ``agent`` user. The versions it installs are
the pins in :mod:`vs_agent.provider_policy`. The Feature directory is passed to
the image mechanism in :mod:`vs_sandbox.feature_image` as data.
"""

from __future__ import annotations

from pathlib import Path

from vs_agent import provider_policy
from vs_agent.container_python import CONTAINER_PYTHON_HOME, CONTAINER_PYTHON_VERSION
from vs_sandbox.api import LocalFeature

#: ``devcontainer features test`` project folder: ``src/<id>/`` and ``test/<id>/``.
AGENT_FEATURES_PROJECT = Path(__file__).resolve().parent / "images" / "features"

#: The user every agent image runs as; the sandbox remaps its uid at container start.
AGENT_USER = "agent"
AGENT_WORKDIR = "/workspace"


def agent_feature() -> LocalFeature:
    """Return the agent Feature with every version option set from ``provider_policy``."""
    options = {"node_version": provider_policy.NODE_VERSION}
    options |= {
        f"{name}_version": version for name, version in provider_policy.CLI_VERSIONS.items()
    }
    options["python_version"] = CONTAINER_PYTHON_VERSION
    options["python_home"] = CONTAINER_PYTHON_HOME
    return LocalFeature(AGENT_FEATURES_PROJECT / "src" / "vibesys-agent", options=options)
