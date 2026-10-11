"""Building an image from a base plus Dev Container Features, with no Docker daemon."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import (
    FeatureImageBuildError,
    FinalImage,
    LocalFeature,
    OciFeature,
    build_feature_image,
    feature_config,
    finish_dockerfile,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_sandbox.api import FeatureRef

_ID = "sha256:" + "d" * 64
_names = st.from_regex(r"[a-z][a-z0-9-]{0,8}", fullmatch=True)
_options = st.dictionaries(_names, st.text(alphabet="abc 123.", max_size=8), max_size=3)


def _local(root: Path, name: str, options: dict[str, str]) -> LocalFeature:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "devcontainer-feature.json").write_text(
        json.dumps({"id": name, "options": {key: {"type": "string"} for key in options}})
    )
    (directory / "install.sh").write_text("#!/bin/sh\n")
    return LocalFeature(directory, options=options)


@given(
    names=st.lists(_names, unique=True, max_size=5),
    options=_options,
    oci=st.lists(st.just("ghcr.io/example/features/tool:1"), max_size=1),
)
def test_config_lists_every_feature_once_in_the_given_order(
    names: list[str], options: dict[str, str], oci: list[str]
) -> None:
    features: list[FeatureRef] = [LocalFeature(Path("/f") / n, options=options) for n in names]
    features += [OciFeature(reference, options=options) for reference in oci]

    config = feature_config("base:1", features, user="agent")

    keys = [feature.key for feature in features]
    assert config["image"] == "base:1"
    assert list(config["features"]) == keys
    assert config["overrideFeatureInstallOrder"] == keys
    assert config["containerUser"] == "agent"
    assert all(config["features"][key] == options for key in keys)


@given(name=_names)
def test_config_rejects_a_feature_listed_twice(name: str) -> None:
    feature = LocalFeature(Path("/f") / name)

    with pytest.raises(ValueError, match="listed twice"):
        feature_config("base:1", [feature, feature], user="agent")


@pytest.mark.parametrize("user", ["", "Root", "a b", "a;b", "-x", "../x"])
def test_config_rejects_a_user_that_is_not_a_plain_name(user: str) -> None:
    with pytest.raises(ValueError, match="plain user name"):
        feature_config("base:1", [], user=user)


@given(workdir=st.sampled_from(["/workspace", "/srv/work", "/"]), user=_names)
def test_finish_stage_sets_the_user_and_directory_last(workdir: str, user: str) -> None:
    lines = finish_dockerfile(user=user, workdir=workdir).splitlines()

    assert lines[:2] == ["ARG BASE_IMAGE", "FROM ${BASE_IMAGE}"]
    assert lines[-2:] == [f"WORKDIR {workdir}", f"USER {user}"]


@pytest.mark.parametrize("workdir", ["workspace", "/a b", ""])
def test_finish_stage_rejects_a_relative_or_spaced_directory(workdir: str) -> None:
    with pytest.raises(ValueError, match="workdir"):
        finish_dockerfile(user="agent", workdir=workdir)


@dataclass(frozen=True)
class _Outcome:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class _Seen:
    root: Path
    config: dict[str, Any]
    finish: str
    copied: frozenset[str]


class _Runner:
    """Records Docker commands; reads the workspace when the CLI container is "run"."""

    def __init__(self, *, fail_on: str | None = None, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.seen: list[_Seen] = []
        self.fail_on = fail_on
        self.error = error

    def run(self, argv: Sequence[str], *, cwd: Path, timeout: float) -> _Outcome:
        del cwd, timeout
        call = tuple(argv)
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        if call[1] == "run":
            root = Path(call[call.index("--workspace-folder") + 1])
            features = root / ".devcontainer" / "features"
            self.seen.append(
                _Seen(
                    root,
                    json.loads((root / ".devcontainer" / "devcontainer.json").read_text()),
                    (root / "finish.Dockerfile").read_text(),
                    frozenset(p.name for p in features.iterdir())
                    if features.exists()
                    else frozenset(),
                )
            )
        if self.fail_on == call[1]:
            return _Outcome(3, "", "boom")
        stdout = _ID if call[1:3] == ("image", "inspect") else ""
        return _Outcome(0, stdout, "")


def _build(runner: _Runner, features: Sequence[FeatureRef]) -> str:
    return build_feature_image(
        "base:1",
        features,
        FinalImage(user="agent", workdir="/workspace", image_repository="demo-build"),
        command_runner=runner,
        timeout=7,
    ).image_id


def test_build_runs_the_cli_in_a_container_then_finishes_and_untags(tmp_path: Path) -> None:
    runner = _Runner()
    feature = _local(tmp_path, "tool-a", {"v": "1"})

    assert _build(runner, [feature]) == _ID

    assert [call[1:3] if call[1] == "image" else call[1:2] for call in runner.calls] == [
        ("build",),  # the devcontainer CLI image: a layer-cache hit when unchanged
        ("run",),  # devcontainer build, in that image
        ("build",),  # the finishing stage
        ("image", "inspect"),
        ("image", "rm"),  # drops the intermediate tag only
    ]
    (seen,) = runner.seen
    root = str(seen.root)
    cli = runner.calls[1]
    assert f"{root}:{root}" in cli  # the workspace is mounted at the same path
    assert "/var/run/docker.sock:/var/run/docker.sock" in cli
    assert seen.config["features"] == {"./features/tool-a": {"v": "1"}}
    assert seen.copied == {"tool-a"}
    assert seen.finish.splitlines()[-2:] == ["WORKDIR /workspace", "USER agent"]
    features_tag = cli[cli.index("--image-name") + 1]
    assert f"BASE_IMAGE={features_tag}" in runner.calls[2]
    assert runner.calls[-1] == ("docker", "image", "rm", features_tag)
    assert runner.calls[2][runner.calls[2].index("--tag") + 1].startswith("demo-build:")
    assert not seen.root.exists()


def test_oci_features_are_referenced_without_copying() -> None:
    runner = _Runner()

    _build(runner, [OciFeature("ghcr.io/example/features/tool:1", options={"a": "b"})])

    assert runner.seen[0].copied == frozenset()
    assert runner.seen[0].config["features"] == {"ghcr.io/example/features/tool:1": {"a": "b"}}


def test_an_option_the_feature_does_not_declare_fails_before_any_docker_command(
    tmp_path: Path,
) -> None:
    runner = _Runner()
    feature = _local(tmp_path, "tool-a", {"v": "1"})
    wrong = LocalFeature(feature.directory, options={"typo": "1"})

    with pytest.raises(FeatureImageBuildError, match="declares no option"):
        _build(runner, [wrong])

    assert runner.calls == []


def test_a_folder_without_feature_files_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FeatureImageBuildError, match="not a Feature"):
        _build(_Runner(), [LocalFeature(tmp_path)])


@pytest.mark.parametrize("failing", ["run", "build"])
def test_a_failed_command_names_the_step_and_cleans_the_workspace(
    tmp_path: Path, failing: str
) -> None:
    runner = _Runner(fail_on=failing)

    with pytest.raises(FeatureImageBuildError, match=r"failed \(exit 3\): boom"):
        _build(runner, [_local(tmp_path, "tool-a", {})])

    if runner.seen:
        assert not runner.seen[0].root.exists()


def test_missing_docker_and_timeouts_are_build_errors(tmp_path: Path) -> None:
    feature = _local(tmp_path, "tool-a", {})

    with pytest.raises(FeatureImageBuildError, match="not found"):
        _build(_Runner(error=FileNotFoundError("docker")), [feature])
    with pytest.raises(FeatureImageBuildError, match="timed out after 7 seconds"):
        _build(_Runner(error=TimeoutError()), [feature])


def test_a_nonpositive_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        build_feature_image(
            "base:1",
            [],
            FinalImage(user="agent", workdir="/workspace", image_repository="demo-build"),
            command_runner=_Runner(),
            timeout=0,
        )
