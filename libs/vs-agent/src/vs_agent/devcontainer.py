"""Describe a run's agent image as a ``devcontainer.json`` of VibeSys Features.

A run's image is its base image plus the Features the run selects. This module
turns the selection (:func:`~vs_agent.api.images.agent_image`'s ``toolchains``
and ``pip_extras``) into the ``devcontainer.json`` the devcontainer CLI builds,
and lays that file and the Feature sources out in a workspace directory.

:func:`devcontainer_config` is pure: the same selection always yields the same
document, whatever order or multiplicity the caller listed it in, so the image
build cache key depends only on what was selected. Every pinned version comes
from :mod:`vs_agent.provider_policy`; the Features' own option defaults repeat
those pins only so ``devcontainer features test`` can run them unconfigured,
and ``tests/test_devcontainer_features.py`` fails if they drift.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from vs_agent import provider_policy
from vs_agent.container_python import CONTAINER_PYTHON_HOME, CONTAINER_PYTHON_VERSION

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

#: Where the Feature sources ship (one directory per Feature id). Resolved
#: relative to this module so it is a real filesystem path Docker can build
#: from, in a source checkout or an installed wheel alike.
FEATURES_SOURCE_DIR = Path(__file__).resolve().parent / "images" / "features" / "src"

#: Toolchain names a run may select, in the order their Features install.
#: Unselected toolchains cost nothing; the names are the public vocabulary of
#: ``agent_image(toolchains=...)``.
RUST_TOOLCHAIN = "rust"
GO_TOOLCHAIN = "go"
_TOOLCHAIN_FEATURES: dict[str, str] = {
    RUST_TOOLCHAIN: "vibesys-rust",
    GO_TOOLCHAIN: "vibesys-go",
    provider_policy.CONTAINER_RUNTIME_TOOLCHAIN: "vibesys-container-runtime",
}
KNOWN_TOOLCHAINS: frozenset[str] = frozenset(_TOOLCHAIN_FEATURES)

#: The ``docker`` group owns the daemon socket the container-runtime Feature
#: installs a daemon for; the agent user joins it when that Feature is selected.
_DOCKER_GROUP = "docker"

#: Where the Feature directories live relative to ``.devcontainer/``, which is
#: where the CLI resolves a ``./features/<id>`` reference from.
_FEATURES_REFERENCE_DIR = "features"


def devcontainer_config(
    base_image: str,
    *,
    toolchains: Collection[str] = (),
    pip_extras: Collection[str] = (),
) -> dict[str, object]:
    """Return the ``devcontainer.json`` document for one run's agent image.

    The agent Feature is always present. Each selected toolchain adds its
    Feature, and a non-empty ``pip_extras`` adds the pip-extras Feature.
    Selections are deduplicated and sorted, so their order never changes the
    result. Features appear in a fixed order, least-often-changed first, so a
    change to a later Feature rebuilds as few layers as possible.

    Raises:
        ValueError: A toolchain name is not one of :data:`KNOWN_TOOLCHAINS`, or
            a pip requirement is empty or contains whitespace (requirements are
            passed to ``pip`` as separate words).
    """
    selected = set(toolchains)
    unknown = selected - KNOWN_TOOLCHAINS
    if unknown:
        message = (
            f"unknown toolchain {sorted(unknown)!r}; the agent image provides "
            f"{sorted(KNOWN_TOOLCHAINS)!r}"
        )
        raise ValueError(message)
    requirements = sorted(set(pip_extras))
    for requirement in requirements:
        if not requirement or requirement.split() != [requirement]:
            message = f"pip requirement must be one word without whitespace: {requirement!r}"
            raise ValueError(message)

    features: dict[str, dict[str, str]] = {
        "vibesys-agent": _agent_options(
            groups=(_DOCKER_GROUP,) if provider_policy.CONTAINER_RUNTIME_TOOLCHAIN in selected else ()
        )
    }
    if requirements:
        features["vibesys-pip-extras"] = {"requirements": " ".join(requirements)}
    options = _toolchain_options()
    for toolchain, feature in _TOOLCHAIN_FEATURES.items():
        if toolchain in selected:
            features[feature] = options[feature]
    return {
        "image": base_image,
        "features": {f"./{_FEATURES_REFERENCE_DIR}/{name}": opts for name, opts in features.items()},
        "containerUser": "agent",
    }


def _agent_options(*, groups: tuple[str, ...]) -> dict[str, str]:
    options = {"node_version": provider_policy.NODE_VERSION}
    options |= {f"{p}_version": v for p, v in provider_policy.CLI_VERSIONS.items()}
    options["python_version"] = CONTAINER_PYTHON_VERSION
    options["python_home"] = str(CONTAINER_PYTHON_HOME)
    options["supplementary_groups"] = " ".join(groups)
    return options


def _toolchain_options() -> Mapping[str, dict[str, str]]:
    return {
        "vibesys-rust": {"rust_version": provider_policy.RUST_TOOLCHAIN_VERSION},
        "vibesys-go": {"go_version": provider_policy.GO_TOOLCHAIN_VERSION},
        "vibesys-container-runtime": {
            "docker_engine_version": provider_policy.DOCKER_ENGINE_VERSION,
            "docker_compose_version": provider_policy.DOCKER_COMPOSE_VERSION,
            "kind_version": provider_policy.KIND_VERSION,
            "kubectl_version": provider_policy.KUBECTL_VERSION,
        },
    }


def write_workspace(root: Path, config: Mapping[str, object]) -> Path:
    """Lay out *config* and the Feature sources under *root*; return the config path.

    The devcontainer CLI resolves ``./features/<id>`` relative to the
    ``.devcontainer/`` directory, so the sources are copied beside the file.
    """
    devcontainer_dir = root / ".devcontainer"
    devcontainer_dir.mkdir(parents=True)
    shutil.copytree(FEATURES_SOURCE_DIR, devcontainer_dir / _FEATURES_REFERENCE_DIR)
    config_path = devcontainer_dir / "devcontainer.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path
