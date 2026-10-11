"""Spike Dev Container Features under ``vs_agent/images/features`` are well formed.

A Feature is a directory, and the directory contents are its public contract:
``devcontainer-feature.json`` plus ``install.sh``. The real builds and
``devcontainer features test`` runs are evidence in the spike PR, not CI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from vs_agent.api import (
    CLI_VERSIONS,
    CONTAINER_PYTHON_HOME,
    CONTAINER_PYTHON_VERSION,
    NODE_VERSION,
    RUST_TOOLCHAIN_VERSION,
)

FEATURES_SRC = Path(__file__).parents[1] / "src" / "vs_agent" / "images" / "features" / "src"
FEATURE_DIRS = sorted(path for path in FEATURES_SRC.iterdir() if path.is_dir())
FEATURE_IDS = {path.name for path in FEATURE_DIRS}

_ORDERING_KEYS = ("installsAfter", "dependsOn")
_ALLOWED_KEYS = {
    "id", "version", "name", "description", "documentationURL", "options", "containerEnv",
    *_ORDERING_KEYS,
}  # fmt: skip


def _metadata(feature_dir: Path) -> dict[str, Any]:
    loaded = json.loads((feature_dir / "devcontainer-feature.json").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _declared_ids(metadata: dict[str, Any]) -> set[str]:
    declared: set[str] = set()
    for key in _ORDERING_KEYS:
        value = metadata.get(key, [])
        declared |= set(value)
    return declared


def test_spike_features_exist() -> None:
    assert {"vibesys-agent", "vibesys-rust"} <= FEATURE_IDS


@pytest.mark.parametrize("feature_dir", FEATURE_DIRS, ids=lambda p: p.name)
def test_metadata_parses_and_names_its_directory(feature_dir: Path) -> None:
    metadata = _metadata(feature_dir)
    assert metadata["id"] == feature_dir.name
    assert set(metadata) <= _ALLOWED_KEYS, set(metadata) - _ALLOWED_KEYS
    assert (feature_dir / "install.sh").is_file()
    assert (feature_dir / "install.sh").stat().st_mode & 0o111


@pytest.mark.parametrize("feature_dir", FEATURE_DIRS, ids=lambda p: p.name)
def test_no_undeclared_ordering_dependency(feature_dir: Path) -> None:
    """An install script names no other Feature unless the metadata declares the ordering."""
    metadata = _metadata(feature_dir)
    declared = _declared_ids(metadata)
    assert declared <= FEATURE_IDS
    script = (feature_dir / "install.sh").read_text()
    mentioned = {
        other for other in FEATURE_IDS - {feature_dir.name} if re.search(re.escape(other), script)
    }
    assert mentioned <= declared, mentioned - declared


@pytest.mark.parametrize("feature_dir", FEATURE_DIRS, ids=lambda p: p.name)
def test_every_option_has_a_default_and_is_read_by_the_script(feature_dir: Path) -> None:
    options = _metadata(feature_dir).get("options", {})
    assert isinstance(options, dict)
    script = (feature_dir / "install.sh").read_text()
    for name, spec in options.items():
        assert "default" in spec, name
        assert name.upper() in script, name


def test_option_defaults_mirror_provider_policy() -> None:
    """The one source of each pin is ``provider_policy``; Feature defaults must equal it."""
    agent = _metadata(FEATURES_SRC / "vibesys-agent")["options"]
    rust = _metadata(FEATURES_SRC / "vibesys-rust")["options"]
    defaults = {name: spec["default"] for name, spec in {**agent, **rust}.items()}
    expected = {
        "node_version": NODE_VERSION,
        "python_version": CONTAINER_PYTHON_VERSION,
        "python_home": CONTAINER_PYTHON_HOME,
        "rust_version": RUST_TOOLCHAIN_VERSION,
        **{f"{p}_version": v for p, v in CLI_VERSIONS.items()},
    }
    assert {k: defaults[k] for k in expected} == expected
