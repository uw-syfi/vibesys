"""Tests for the agent image build (:func:`vs_agent.api.images.agent_image`).

``build_task_image`` itself is covered by ``test_task_image.py``. This file
covers the agent layer: the commands it issues (one ``docker build`` of the
Dockerfile generated from Features, then an inspect), the build context it
hands Docker, how it chains onto a task image, and that the files it builds
from ship with the package.
"""

from __future__ import annotations

import importlib.resources
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api.images import (
    AGENT_FEATURES_PROJECT,
    DockerCommandResult,
    TaskImageBuildError,
    agent_image,
)
from vs_agent.api.testing import DockerBuildResult, docker_build_result
from vs_sandbox.api import LocalFeature
from vs_sim.api import DISCARD
from vs_sim.api.testing import ManualClock

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_sim.api import OutputSink

_TASK_IMAGE_ID = "sha256:" + "a" * 64
_AGENT_IMAGE_ID = "sha256:" + "b" * 64
_BARE_ID = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class _Workspace:
    """What ``docker build`` would have seen for the agent image."""

    root: Path
    base_image: str
    dockerfile: str
    feature_ids: tuple[str, ...]


class _FakeRunner:
    """Answers Docker commands and records each one in order.

    When the agent image is built, its build context is read and kept, because
    the real one is deleted after the build.
    """

    def __init__(
        self,
        *,
        task_image_id: str = _TASK_IMAGE_ID,
        agent_image_id: str = _AGENT_IMAGE_ID,
        agent_inspect_omitted_characters: int = 0,
        build_returncode: int = 0,
    ) -> None:
        self.task_image_id = task_image_id
        self.agent_image_id = agent_image_id
        self.agent_inspect_omitted_characters = agent_inspect_omitted_characters
        self.build_returncode = build_returncode
        self.calls: list[tuple[tuple[str, ...], Path, float]] = []
        self.workspaces: list[_Workspace] = []
        self.images: set[str] = set()

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        output: OutputSink,
    ) -> DockerBuildResult:
        del output
        normalized = tuple(argv)
        self.calls.append((normalized, cwd, timeout))
        if normalized[1] == "build":
            if self.build_returncode == 0:
                self.images.add(normalized[normalized.index("--tag") + 1])
            if any(a.startswith("vibesys-agent-build:") for a in argv):
                self._read_workspace(normalized)
                return docker_build_result(("docker",), self.build_returncode, "", "build failed")
        if normalized[1] in {"build", "image"} and normalized[2] != "inspect":
            return docker_build_result(("docker",), 0, "", "")
        if normalized[1] == "image":
            tag = normalized[-1]
            if tag not in self.images:
                return docker_build_result(("docker",), 1, "", f"No such image: {tag}")
            image_id = (
                self.task_image_id if tag.startswith("vibesys-task-build:") else self.agent_image_id
            )
            omitted = (
                self.agent_inspect_omitted_characters
                if tag.startswith("vibesys-agent-build:")
                else 0
            )
            return DockerCommandResult(
                args=("docker",),
                returncode=0,
                stdout=image_id,
                stderr="",
                stdout_omitted_characters=omitted,
            )
        return docker_build_result(("docker",), 0, "", "")

    def _read_workspace(self, argv: tuple[str, ...]) -> None:
        root = Path(argv[-1])
        base = next(a for a in argv if a.startswith("BASE_IMAGE=")).removeprefix("BASE_IMAGE=")
        dockerfile = Path(argv[argv.index("--file") + 1]).read_text()
        self.workspaces.append(
            _Workspace(
                root=root,
                base_image=base,
                dockerfile=dockerfile,
                feature_ids=tuple(
                    line.split()[1].removeprefix("features/")
                    for line in dockerfile.splitlines()
                    if line.startswith("COPY ")
                ),
            )
        )

    def verbs(self) -> list[tuple[str, ...]]:
        """The first words of each command, for asserting the order of the build."""
        return [call[0][1:3] if call[0][1] == "image" else call[0][1:2] for call in self.calls]


def test_agent_image_is_one_docker_build_of_the_generated_dockerfile() -> None:
    runner = _FakeRunner()

    image_id = agent_image(
        "python:3.12-bookworm",
        command_runner=runner,
        timeout=42,
        clock=ManualClock(),
        output=DISCARD,
    )

    assert image_id == _AGENT_IMAGE_ID
    assert runner.verbs() == [
        ("image", "inspect"),
        ("image", "inspect"),
        ("build",),
        ("image", "inspect"),
    ]
    assert {timeout for _argv, _cwd, timeout in runner.calls} == {42}
    (workspace,) = runner.workspaces
    assert workspace.base_image == "python:3.12-bookworm"
    assert workspace.feature_ids == ("vibesys-agent",)
    assert not workspace.root.exists()  # the build context does not outlive the build


def test_a_second_launch_of_an_unchanged_selection_builds_and_exports_nothing(
    tmp_path: Path,
) -> None:
    """Regression: every launch ran ``docker build``, and a build re-exports the whole image.

    A build whose layers are all cached still writes the image out, and on a host
    whose daemon lacks the base that export took minutes; the image tag names every
    input, so a daemon that has the tag has the image.
    """
    task_root = tmp_path / "task"
    task_root.mkdir()
    (task_root / "Dockerfile").write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n", encoding="utf-8")
    runner = _FakeRunner()

    def launch() -> str:
        return agent_image(
            "python:3.12-bookworm",
            task_dockerfile=task_root / "Dockerfile",
            command_runner=runner,
            clock=ManualClock(),
            output=DISCARD,
        )

    first = launch()
    builds_after_first = sum(call[0][1] == "build" for call in runner.calls)
    second = launch()

    assert first == second == _AGENT_IMAGE_ID
    assert builds_after_first == 2  # the task image, then the agent layer
    assert sum(call[0][1] == "build" for call in runner.calls) == builds_after_first


def test_the_image_runs_as_agent_in_workspace_and_never_mounts_the_docker_socket() -> None:
    """The Dockerfile ends with WORKDIR and USER, and no command runs a container or mounts a path."""
    runner = _FakeRunner()

    agent_image("python:3.12-bookworm", command_runner=runner, clock=ManualClock(), output=DISCARD)

    assert runner.workspaces[0].dockerfile.splitlines()[-2:] == [
        "WORKDIR /workspace",
        "USER agent",
    ]
    for argv, _cwd, _timeout in runner.calls:
        assert argv[1] != "run"
        assert not any("docker.sock" in token for token in argv)


def test_agent_image_chains_task_image_as_base(tmp_path: Path) -> None:
    """A task Dockerfile is built first; its unique local tag becomes the devcontainer
    base (BuildKit cannot resolve a bare image ID), and BASE_IMAGE is also forwarded
    into the task build so a task Dockerfile may ``FROM ${BASE_IMAGE}``.
    """
    task_root = tmp_path / "task"
    task_root.mkdir()
    task_dockerfile = task_root / "Dockerfile"
    task_dockerfile.write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n", encoding="utf-8")
    runner = _FakeRunner()

    image_id = agent_image(
        "python:3.12-bookworm",
        task_dockerfile=task_dockerfile,
        command_runner=runner,
        clock=ManualClock(),
        output=DISCARD,
    )

    assert image_id == _AGENT_IMAGE_ID
    task_build_argv, task_cwd, _ = runner.calls[1]
    task_tag = task_build_argv[4]
    assert task_tag.startswith("vibesys-task-build:")
    assert task_build_argv == (
        "docker",
        "build",
        "--provenance=false",
        "--tag",
        task_tag,
        "--file",
        str(task_dockerfile),
        "--build-arg",
        "BASE_IMAGE=python:3.12-bookworm",
        str(task_root),
    )
    assert task_cwd == task_root
    assert runner.calls[2][0] == (
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        task_tag,
    )
    assert runner.workspaces[0].base_image == task_tag


def test_extra_features_follow_the_agent_feature_in_order(tmp_path: Path) -> None:
    extras = []
    for name in ("zeta", "alpha"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "devcontainer-feature.json").write_text(f'{{"id": "{name}"}}')
        (directory / "install.sh").write_text("#!/bin/sh\n")
        extras.append(LocalFeature(directory))
    runner = _FakeRunner()

    agent_image(
        "python:3.12-bookworm",
        features=extras,
        command_runner=runner,
        clock=ManualClock(),
        output=DISCARD,
    )

    assert runner.workspaces[0].feature_ids == ("vibesys-agent", "zeta", "alpha")


def test_agent_image_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        agent_image(
            "python:3.12-bookworm",
            command_runner=_FakeRunner(),
            timeout=0,
            clock=ManualClock(),
            output=DISCARD,
        )


def test_a_failed_feature_image_build_is_a_task_image_build_error_and_cleans_up() -> None:
    runner = _FakeRunner(build_returncode=1)

    with pytest.raises(TaskImageBuildError, match=r"feature image build failed.*build failed"):
        agent_image(
            "python:3.12-bookworm", command_runner=runner, clock=ManualClock(), output=DISCARD
        )

    assert runner.verbs() == [("image", "inspect"), ("image", "inspect"), ("build",)]
    assert not runner.workspaces[0].root.exists()


def test_agent_image_rejects_a_truncated_inspect_id() -> None:
    runner = _FakeRunner(agent_inspect_omitted_characters=2048)

    with pytest.raises(TaskImageBuildError, match="characters truncated"):
        agent_image(
            "python:3.12-bookworm", command_runner=runner, clock=ManualClock(), output=DISCARD
        )


def test_agent_feature_ships_as_package_data() -> None:
    """The Feature resolves through ``importlib.resources`` against the installed package,
    so a wheel that forgot to declare the data fails here."""
    feature = importlib.resources.files("vs_agent").joinpath(
        "images", "features", "src", "vibesys-agent"
    )
    assert feature.joinpath("install.sh").is_file()
    assert feature.joinpath("devcontainer-feature.json").is_file()
    assert AGENT_FEATURES_PROJECT.joinpath("src", "vibesys-agent").is_dir()


def _devcontainer_base(runner: _FakeRunner) -> str:
    return runner.workspaces[0].base_image


@given(
    base_image=st.one_of(
        st.from_regex(r"sha256:[0-9a-f]{64}", fullmatch=True),
        st.sampled_from(["python:3.12-bookworm", "ubuntu:24.04", "ghcr.io/x/y@sha256:" + "c" * 64]),
    ),
    flow=st.sampled_from(["base-only", "task-dockerfile"]),
)
def test_agent_base_image_is_never_a_bare_image_id(base_image: str, flow: str) -> None:
    """BuildKit resolves ``FROM sha256:<id>`` as a registry reference and fails
    (#1520), so the devcontainer base is a tag for every input."""
    with tempfile.TemporaryDirectory() as tmp:
        task_dockerfile = None
        if flow == "task-dockerfile":
            task_dockerfile = Path(tmp) / "Dockerfile"
            task_dockerfile.write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n", encoding="utf-8")
        runner = _FakeRunner()

        agent_image(
            base_image,
            task_dockerfile=task_dockerfile,
            command_runner=runner,
            clock=ManualClock(),
            output=DISCARD,
        )

        agent_base = _devcontainer_base(runner)
        assert _BARE_ID.fullmatch(agent_base) is None
        if task_dockerfile is not None:
            assert agent_base.startswith("vibesys-task-build:")
        elif _BARE_ID.fullmatch(base_image):
            tag_calls = [argv for argv, _c, _t in runner.calls if argv[1] == "tag"]
            assert tag_calls == [("docker", "tag", base_image, agent_base)]
        else:
            assert agent_base == base_image


def test_agent_image_tags_a_bare_base_image_id_before_building() -> None:
    """The entrypoint records the task image by ID; the agent layer tags it."""
    runner = _FakeRunner()

    agent_image(_TASK_IMAGE_ID, command_runner=runner, clock=ManualClock(), output=DISCARD)

    tag = "vibesys-base:" + "a" * 64
    assert runner.calls[0][0] == ("docker", "tag", _TASK_IMAGE_ID, tag)
    assert _devcontainer_base(runner) == tag


def test_agent_builder_accepts_explicit_cache_sources() -> None:
    runner = _FakeRunner()
    sources = ("cache:first", "cache:second")
    agent_image(
        "python:3.12-bookworm",
        cache_from=sources,
        command_runner=runner,
        clock=ManualClock(),
        output=DISCARD,
    )
    (build,) = [argv for argv, _, _ in runner.calls if argv[1] == "build"]
    assert tuple(build[i + 1] for i, arg in enumerate(build) if arg == "--cache-from") == sources


def test_invalid_cache_sources_fail_before_tagging_a_base_image() -> None:
    runner = _FakeRunner()
    with pytest.raises(ValueError, match="cache_from"):
        agent_image(
            _TASK_IMAGE_ID,
            cache_from=("",),
            command_runner=runner,
            clock=ManualClock(),
            output=DISCARD,
        )
    assert runner.calls == []
