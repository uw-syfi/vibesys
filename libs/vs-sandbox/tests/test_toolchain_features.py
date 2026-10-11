"""The toolchain catalog selects well-formed Features pinned in one place."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import (
    CONTAINER_RUNTIME_TOOLCHAIN,
    DOCKER_COMPOSE_VERSION,
    DOCKER_ENGINE_VERSION,
    GO_TOOLCHAIN_VERSION,
    KIND_VERSION,
    KNOWN_TOOLCHAINS,
    KUBECTL_VERSION,
    RUST_TOOLCHAIN_VERSION,
    TOOLCHAIN_FEATURES_PROJECT,
    feature_project_problems,
    toolchain_features,
)

if TYPE_CHECKING:
    from pathlib import Path

_toolchains = st.lists(st.sampled_from(KNOWN_TOOLCHAINS), max_size=6)
_PINS = {
    "rust_version": RUST_TOOLCHAIN_VERSION,
    "go_version": GO_TOOLCHAIN_VERSION,
    "docker_engine_version": DOCKER_ENGINE_VERSION,
    "docker_compose_version": DOCKER_COMPOSE_VERSION,
    "kind_version": KIND_VERSION,
    "kubectl_version": KUBECTL_VERSION,
}


def _declared(feature_dir: Path) -> dict[str, Any]:
    return json.loads((feature_dir / "devcontainer-feature.json").read_text())["options"]


def test_the_catalog_project_is_well_formed() -> None:
    assert feature_project_problems(TOOLCHAIN_FEATURES_PROJECT) == []


@given(toolchains=_toolchains, data=st.data())
def test_selection_ignores_order_and_repeats(toolchains: list[str], data: st.DataObject) -> None:
    shuffled = data.draw(st.permutations([*toolchains, *toolchains]))

    assert toolchain_features(shuffled) == toolchain_features(toolchains)


@given(toolchains=_toolchains)
def test_selection_is_exactly_the_requested_toolchains_in_catalog_order(
    toolchains: list[str],
) -> None:
    ids = [f.feature_id for f in toolchain_features(toolchains)]

    assert ids == [f"vibesys-{name}" for name in KNOWN_TOOLCHAINS if name in toolchains]


@given(toolchains=_toolchains, users=st.lists(st.sampled_from(["agent", "ci"]), max_size=3))
def test_options_are_declared_pinned_and_name_the_docker_group_users(
    toolchains: list[str], users: list[str]
) -> None:
    for feature in toolchain_features(toolchains, docker_group_users=users):
        declared = _declared(feature.directory)
        assert set(feature.options) <= set(declared)
        for name, value in feature.options.items():
            if name == "users":
                assert feature.feature_id == f"vibesys-{CONTAINER_RUNTIME_TOOLCHAIN}"
                assert value.split() == sorted(set(users))
            else:
                assert value == declared[name]["default"] == _PINS[name]


@given(name=st.text(min_size=1, max_size=12).filter(lambda n: n not in KNOWN_TOOLCHAINS))
def test_unknown_toolchain_is_rejected_by_name(name: str) -> None:
    with pytest.raises(ValueError, match="unknown toolchain") as excinfo:
        toolchain_features(["rust", name])
    assert repr(name) in str(excinfo.value)
