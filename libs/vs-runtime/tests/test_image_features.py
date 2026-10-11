"""A run environment's toolchains and pip extras select the right Features."""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api.images import AGENT_USER
from vs_runtime.api.infrastructure import RUN_ENVIRONMENT_FEATURES_PROJECT, image_features
from vs_sandbox.api import KNOWN_TOOLCHAINS, LocalFeature, feature_project_problems

_toolchains = st.lists(st.sampled_from(KNOWN_TOOLCHAINS), max_size=6)
_requirements = st.lists(
    st.from_regex(r"[A-Za-z][A-Za-z0-9_.\-]{0,10}([<>=]=?[0-9.]{1,5})?", fullmatch=True),
    max_size=4,
)


def _ids(features: tuple[Any, ...]) -> list[str]:
    return [f.feature_id for f in features]


def test_the_run_environment_project_is_well_formed() -> None:
    assert feature_project_problems(RUN_ENVIRONMENT_FEATURES_PROJECT) == []


@given(toolchains=_toolchains, extras=_requirements)
def test_selection_is_the_extras_feature_then_the_toolchains(
    toolchains: list[str], extras: list[str]
) -> None:
    ids = _ids(image_features(toolchains, extras))

    expected = ["vibesys-pip-extras"] if extras else []
    expected += [f"vibesys-{name}" for name in KNOWN_TOOLCHAINS if name in toolchains]
    assert ids == expected


@given(toolchains=_toolchains, extras=_requirements, data=st.data())
def test_order_and_repeats_of_the_selection_do_not_change_the_result(
    toolchains: list[str], extras: list[str], data: st.DataObject
) -> None:
    shuffled = data.draw(st.permutations([*toolchains, *toolchains]))
    shuffled_extras = data.draw(st.permutations([*extras, *extras]))

    assert image_features(shuffled, shuffled_extras) == image_features(toolchains, extras)


@given(toolchains=_toolchains, extras=_requirements)
def test_every_option_is_declared_by_its_feature(toolchains: list[str], extras: list[str]) -> None:
    for feature in image_features(toolchains, extras):
        assert isinstance(feature, LocalFeature)
        metadata = json.loads((feature.directory / "devcontainer-feature.json").read_text())
        assert set(feature.options) <= set(metadata["options"])


@given(extras=_requirements.filter(bool))
def test_pip_requirements_are_passed_sorted_and_deduplicated(extras: list[str]) -> None:
    (feature,) = image_features((), [*extras, *extras])

    assert feature.options["requirements"].split() == sorted(set(extras))


def test_the_agent_user_joins_the_docker_group_with_the_container_runtime() -> None:
    (feature,) = image_features(["container-runtime"])

    assert feature.options["users"] == AGENT_USER


@pytest.mark.parametrize("requirement", ["", "modal >= 0.66", "a b", " x", "x\n"])
def test_pip_requirement_must_be_one_word(requirement: str) -> None:
    with pytest.raises(ValueError, match="pip requirement"):
        image_features((), [requirement])


def test_an_unknown_toolchain_is_rejected_by_name() -> None:
    with pytest.raises(ValueError, match="'cobol'"):
        image_features(["cobol"])
