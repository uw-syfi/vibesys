"""The generated ``devcontainer.json`` selects exactly the Features a run asked for."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api.images import KNOWN_TOOLCHAINS, devcontainer_config

FEATURES_SRC = Path(__file__).parents[1] / "src" / "vs_agent" / "images" / "features" / "src"
#: Options a run chooses; every other option is a pin whose value is the Feature default.
_SELECTION_OPTIONS = {"requirements", "supplementary_groups"}

_toolchains = st.lists(st.sampled_from(sorted(KNOWN_TOOLCHAINS)), max_size=6)
_requirements = st.lists(
    st.from_regex(r"[A-Za-z][A-Za-z0-9_.\-]{0,10}([<>=]=?[0-9.]{1,5})?", fullmatch=True),
    max_size=4,
)


def _features(config: dict[str, object]) -> dict[str, dict[str, str]]:
    features = config["features"]
    assert isinstance(features, dict)
    return {key.removeprefix("./features/"): options for key, options in features.items()}


def _features_by_key(config: dict[str, object]) -> dict[str, object]:
    features = config["features"]
    assert isinstance(features, dict)
    return features


def _declared(feature: str) -> dict[str, Any]:
    metadata = json.loads((FEATURES_SRC / feature / "devcontainer-feature.json").read_text())
    return metadata["options"]


@given(toolchains=_toolchains, extras=_requirements, data=st.data())
def test_selection_order_and_repeats_do_not_change_the_document(
    toolchains: list[str], extras: list[str], data: st.DataObject
) -> None:
    shuffled_toolchains = data.draw(st.permutations(toolchains))
    shuffled_extras = data.draw(st.permutations(extras))

    plain = devcontainer_config("base:1", toolchains=toolchains, pip_extras=extras)
    again = devcontainer_config(
        "base:1",
        toolchains=[*shuffled_toolchains, *toolchains],
        pip_extras=[*shuffled_extras, *extras],
    )

    assert json.dumps(plain) == json.dumps(again)


@given(toolchains=_toolchains, extras=_requirements)
def test_features_are_the_agent_plus_exactly_the_selection(
    toolchains: list[str], extras: list[str]
) -> None:
    config = devcontainer_config("base:1", toolchains=toolchains, pip_extras=extras)

    expected = {"vibesys-agent"} | {f"vibesys-{name}" for name in toolchains}
    if extras:
        expected.add("vibesys-pip-extras")
    assert set(_features(config)) == expected
    assert config["image"] == "base:1"
    for feature in expected:
        assert (FEATURES_SRC / feature / "install.sh").is_file(), feature


@given(toolchains=_toolchains, extras=_requirements)
def test_every_option_is_declared_and_pins_equal_the_feature_defaults(
    toolchains: list[str], extras: list[str]
) -> None:
    """The generator cannot pass an option a Feature ignores, and cannot drift from the pins."""
    for feature, options in _features(
        devcontainer_config("base:1", toolchains=toolchains, pip_extras=extras)
    ).items():
        declared = _declared(feature)
        assert set(options) <= set(declared), feature
        for name, value in options.items():
            if name not in _SELECTION_OPTIONS:
                assert value == declared[name]["default"], (feature, name)


@given(toolchains=_toolchains)
def test_agent_joins_the_docker_group_exactly_with_the_container_runtime(
    toolchains: list[str],
) -> None:
    agent = _features(devcontainer_config("base:1", toolchains=toolchains))["vibesys-agent"]

    assert agent["supplementary_groups"] == ("docker" if "container-runtime" in toolchains else "")


@given(extras=_requirements.filter(bool))
def test_pip_requirements_are_passed_sorted_and_deduplicated(extras: list[str]) -> None:
    config = devcontainer_config("base:1", pip_extras=[*extras, *extras])

    passed = _features(config)["vibesys-pip-extras"]["requirements"]
    assert passed.split() == sorted(set(extras))


@given(name=st.text(min_size=1, max_size=12).filter(lambda n: n not in KNOWN_TOOLCHAINS))
def test_unknown_toolchain_is_rejected_by_name(name: str) -> None:
    with pytest.raises(ValueError, match="unknown toolchain") as excinfo:
        devcontainer_config("base:1", toolchains=["rust", name])
    assert repr(name) in str(excinfo.value)


@pytest.mark.parametrize("requirement", ["", "modal >= 0.66", "a b", " x", "x\n"])
def test_pip_requirement_must_be_one_word(requirement: str) -> None:
    with pytest.raises(ValueError, match="pip requirement"):
        devcontainer_config("base:1", pip_extras=[requirement])


@given(toolchains=_toolchains, extras=_requirements)
def test_install_order_is_explicit_and_covers_every_selected_feature(
    toolchains: list[str], extras: list[str]
) -> None:
    config = devcontainer_config("base:1", toolchains=toolchains, pip_extras=extras)

    order = config["overrideFeatureInstallOrder"]
    assert isinstance(order, list)
    assert order == list(_features_by_key(config))
    assert order[0] == "./features/vibesys-agent"
