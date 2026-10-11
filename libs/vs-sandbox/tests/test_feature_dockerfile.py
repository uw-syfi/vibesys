"""The Dockerfile generated from Dev Container Features, checked as pure data."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import (
    BuildPlan,
    FeatureInstall,
    FeatureSpec,
    FeatureSpecError,
    option_env_name,
    parse_feature_metadata,
    plan_feature_build,
)

_ids = st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True)
_option_ids = st.from_regex(r"[a-z][a-z_]{0,8}", fullmatch=True)
# Characters that matter to a shell or to Dockerfile syntax must survive.
_values = st.text(alphabet="abc 123.$\"'\\`;&|<>(){}#*~!-_/=", max_size=12)
_env_values = st.text(alphabet='abc 123.$"\\{}:/-_', max_size=12)

_UNSUPPORTED_FIELDS = [
    "mounts", "privileged", "init", "capAdd", "securityOpt", "entrypoint", "customizations",
    "onCreateCommand", "postCreateCommand", "legacyIds", "deprecated", "extraneous",
]  # fmt: skip


def _spec(
    feature_id: str,
    defaults: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    installs_after: tuple[str, ...] = (),
) -> FeatureSpec:
    return FeatureSpec(feature_id, defaults or {}, env or {}, installs_after)


def _install(
    feature_id: str,
    options: dict[str, str] | None = None,
    *,
    defaults: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    installs_after: tuple[str, ...] = (),
) -> FeatureInstall:
    spec = _spec(feature_id, defaults, env, installs_after)
    return FeatureInstall(spec, Path("/src") / feature_id, options or {})


def _plan(installs: list[FeatureInstall]) -> BuildPlan:
    return plan_feature_build(installs, user="agent", workdir="/workspace")


def _parse_env_file(text: str) -> dict[str, str]:
    parsed = {}
    for line in text.splitlines():
        name, _, quoted = line.partition("=")
        (value,) = shlex.split(quoted) if quoted else ("",)
        parsed[name] = value
    return parsed


@given(ids=st.lists(_ids, unique=True, min_size=1, max_size=5))
def test_features_install_in_the_given_order_then_the_image_starts_as_the_user(
    ids: list[str],
) -> None:
    plan = _plan([_install(feature_id) for feature_id in ids])

    lines = plan.dockerfile.splitlines()
    copies = [line.split()[1] for line in lines if line.startswith("COPY ")]
    assert copies == [f"features/{feature_id}" for feature_id in ids]
    assert list(plan.copied_folders) == copies
    assert lines[:3] == ["ARG BASE_IMAGE", "FROM ${BASE_IMAGE}", "USER root"]
    assert lines[-2:] == ["WORKDIR /workspace", "USER agent"]
    assert lines.count(" && ./install.sh \\") == len(ids)


@given(ids=st.lists(_ids, unique=True, min_size=1, max_size=4))
def test_the_plan_is_deterministic(ids: list[str]) -> None:
    installs = [_install(feature_id) for feature_id in ids]

    assert _plan(installs) == _plan(list(installs))


@given(
    option_ids=st.lists(_option_ids, unique=True, min_size=1, max_size=4),
    data=st.data(),
)
def test_every_declared_option_is_exported_with_the_given_value_or_its_default(
    option_ids: list[str], data: st.DataObject
) -> None:
    defaults = {option: data.draw(_values) for option in option_ids}
    given_options = {
        option: data.draw(_values) for option in option_ids if data.draw(st.booleans())
    }

    plan = _plan([_install("tool", given_options, defaults=defaults)])

    exported = _parse_env_file(plan.generated_files[f"features/tool/{'devcontainer-features.env'}"])
    expected = {**defaults, **given_options}
    assert {option_env_name(o): v for o, v in expected.items()}.items() <= exported.items()
    assert exported["_REMOTE_USER"] == "root"


@given(option_id=st.text(min_size=1, max_size=12))
def test_option_names_become_upper_case_environment_variables(option_id: str) -> None:
    name = option_env_name(option_id)

    assert name == name.upper()
    assert all((ch.isalnum() and ch.isascii()) or ch == "_" for ch in name)
    assert not name[0].isdigit()


@pytest.mark.parametrize(
    ("option_id", "name"),
    [("node_version", "NODE_VERSION"), ("a-b", "A_B"), ("9lives", "_LIVES"), ("__x", "_X")],
)
def test_option_names_follow_the_specification_examples(option_id: str, name: str) -> None:
    assert option_env_name(option_id) == name


@given(option=_option_ids)
def test_an_option_the_feature_does_not_declare_is_rejected_by_name(option: str) -> None:
    with pytest.raises(FeatureSpecError, match=f"declares no option.*{option}"):
        _plan([_install("tool", {option: "1"})])


def test_two_options_that_export_the_same_variable_are_rejected() -> None:
    with pytest.raises(FeatureSpecError, match="collide"):
        _plan([_install("tool", defaults={"a-b": "1", "a_b": "2"})])


@given(values=st.lists(_env_values, min_size=1, max_size=3))
def test_container_env_follows_its_features_install_and_precedes_the_next_copy(
    values: list[str],
) -> None:
    installs = [
        _install(f"tool-{index}", env={f"VAR_{index}": value}) for index, value in enumerate(values)
    ]

    lines = _plan(installs).dockerfile.splitlines()

    for index in range(len(values)):
        copy = next(i for i, line in enumerate(lines) if line == _copy_line(index))
        env = next(i for i, line in enumerate(lines) if line.startswith(f"ENV VAR_{index}="))
        install = next(
            i for i, line in enumerate(lines) if i > copy and line.endswith(f"tool-{index}")
        )
        assert copy < install < env
        if index + 1 < len(values):
            assert env < next(i for i, line in enumerate(lines) if line == _copy_line(index + 1))


def _copy_line(index: int) -> str:
    return f"COPY features/tool-{index} /usr/src/vibesys-features/tool-{index}"


@given(value=_env_values)
def test_container_env_values_are_quoted_and_keep_variable_references(value: str) -> None:
    plan = _plan([_install("tool", env={"PATH": f"/x:{value}:${{PATH}}"})])

    (line,) = (line for line in plan.dockerfile.splitlines() if line.startswith("ENV "))
    body = line.removeprefix('ENV PATH="').removesuffix('"')
    unescaped = body.replace('\\"', '"').replace("\\\\", "\\")
    assert unescaped == f"/x:{value}:${{PATH}}"
    assert unescaped.endswith("${PATH}")


@given(field=st.sampled_from(_UNSUPPORTED_FIELDS))
def test_metadata_with_an_unsupported_field_is_rejected_naming_it(field: str) -> None:
    document = json.dumps({"id": "tool", field: True})

    with pytest.raises(FeatureSpecError, match=f"unsupported metadata field.*{field}"):
        parse_feature_metadata(document, folder_name="tool")


@given(
    extra=st.text(min_size=1, max_size=8).filter(
        lambda key: (
            key
            not in {
                "id",
                "version",
                "name",
                "description",
                "documentationURL",
                "options",
                "containerEnv",
                "installsAfter",
                "dependsOn",
            }
        )
    )
)
def test_any_field_outside_the_supported_set_is_rejected(extra: str) -> None:
    with pytest.raises(FeatureSpecError, match="unsupported metadata field"):
        parse_feature_metadata(json.dumps({"id": "tool", extra: 1}), folder_name="tool")


def test_metadata_is_read_into_defaults_environment_and_order() -> None:
    document = {
        "id": "tool",
        "options": {
            "version": {"type": "string", "default": "1.2"},
            "fast": {"type": "boolean", "default": True},
        },
        "containerEnv": {"A": "b"},
        "installsAfter": ["base"],
    }

    spec = parse_feature_metadata(json.dumps(document), folder_name="tool")

    assert dict(spec.option_defaults) == {"version": "1.2", "fast": "true"}
    assert dict(spec.container_env) == {"A": "b"}
    assert spec.installs_after == ("base",)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("not json", "not valid JSON"),
        ("[]", "must be a JSON object"),
        ('{"id": "other"}', "differs from the folder"),
        ('{"id": "tool", "dependsOn": {"x": {}}}', "dependsOn is not supported"),
        ('{"id": "tool", "options": {"a": {"type": "string"}}}', "has no default"),
        ('{"id": "tool", "options": {"a": {"default": "x"}}}', "needs type"),
        ('{"id": "tool", "containerEnv": {"A": 1}}', "containerEnv"),
    ],
)
def test_malformed_metadata_is_rejected_with_the_reason(document: str, message: str) -> None:
    with pytest.raises(FeatureSpecError, match=message):
        parse_feature_metadata(document, folder_name="tool")


def test_a_feature_must_not_precede_one_it_installs_after() -> None:
    first = _install("late", installs_after=("early",))
    second = _install("early")

    _plan([second, first])
    with pytest.raises(FeatureSpecError, match="must install after early"):
        _plan([first, second])


def test_a_feature_listed_twice_is_rejected() -> None:
    with pytest.raises(FeatureSpecError, match="listed twice"):
        _plan([_install("tool"), _install("tool")])


@pytest.mark.parametrize("user", ["", "Root", "a b", "a;b", "-x", "../x"])
def test_a_user_that_is_not_a_plain_name_is_rejected(user: str) -> None:
    with pytest.raises(FeatureSpecError, match="plain user name"):
        plan_feature_build([], user=user, workdir="/workspace")


@pytest.mark.parametrize("workdir", ["workspace", "/a b", "", '/a"b'])
def test_a_relative_or_spaced_directory_is_rejected(workdir: str) -> None:
    with pytest.raises(FeatureSpecError, match="workdir"):
        plan_feature_build([], user="agent", workdir=workdir)


def test_an_option_value_with_a_newline_is_rejected() -> None:
    with pytest.raises(FeatureSpecError, match="newline"):
        _plan([_install("tool", {"a": "x\ny"}, defaults={"a": ""})])
