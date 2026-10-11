"""Tests for the agent image build (:func:`vs_agent.api.images.agent_image`).

``build_task_image`` itself is covered by ``test_task_image.py``. This file
covers the agent layer: the commands it issues (the containerized devcontainer
CLI, then the finishing stage), the workspace it hands the CLI, how it chains
onto a task image, and that the files it builds from ship with the package.
"""

from __future__ import annotations

import importlib.resources
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api.images import AGENT_FEATURES_PROJECT, TaskImageBuildError, agent_image
from vs_agent.api.testing import DockerResult, docker_result
from vs_sandbox.api import LocalFeature

if TYPE_CHECKING:
    from collections.abc import Sequence

_TASK_IMAGE_ID = "sha256:" + "a" * 64
_AGENT_IMAGE_ID = "sha256:" + "b" * 64
_BARE_ID = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class _Workspace:
    """What the devcontainer CLI would have seen when it was run."""

    root: Path
    config: dict[str, Any]
    finish_dockerfile: str
    feature_ids: frozenset[str]


class _FakeRunner:
    """Answers Docker commands and records each one in order.

    When the devcontainer CLI container is "run", the workspace it was pointed
    at is read and kept, because the real one is deleted after the build.
    """

    def __init__(
        self,
        *,
        task_image_id: str = _TASK_IMAGE_ID,
        agent_image_id: str = _AGENT_IMAGE_ID,
        cli_returncode: int = 0,
    ) -> None:
        self.task_image_id = task_image_id
        self.agent_image_id = agent_image_id
        self.cli_returncode = cli_returncode
        self.calls: list[tuple[tuple[str, ...], Path, float]] = []
        self.workspaces: list[_Workspace] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> DockerResult:
        normalized = tuple(argv)
        self.calls.append((normalized, cwd, timeout))
        if normalized[1] == "run":
            self._read_workspace(normalized)
            return docker_result(("docker",), self.cli_returncode, "", "cli failed")
        if normalized[1] in {"build", "image"} and normalized[2] != "inspect":
            return docker_result(("docker",), 0, "", "")
        if normalized[1] == "image":
            tag = normalized[-1]
            image_id = (
                self.task_image_id if tag.startswith("vibesys-task-build:") else self.agent_image_id
            )
            return docker_result(("docker",), 0, image_id, "")
        return docker_result(("docker",), 0, "", "")

    def _read_workspace(self, argv: tuple[str, ...]) -> None:
        root = Path(argv[argv.index("--workspace-folder") + 1])
        features = root / ".devcontainer" / "features"
        self.workspaces.append(
            _Workspace(
                root=root,
                config=json.loads((root / ".devcontainer" / "devcontainer.json").read_text()),
                finish_dockerfile=(root / "finish.Dockerfile").read_text(),
                feature_ids=frozenset(p.name for p in features.iterdir() if p.is_dir()),
            )
        )

    def verbs(self) -> list[tuple[str, ...]]:
        """The first words of each command, for asserting the order of the build."""
        return [call[0][1:3] if call[0][1] == "image" else call[0][1:2] for call in self.calls]

    def final_build(self) -> tuple[str, ...]:
        return next(
            argv
            for argv, _cwd, _timeout in self.calls
            if argv[1] == "build" and any(a.startswith("vibesys-agent-build:") for a in argv)
        )


def test_agent_image_builds_through_the_containerized_cli_then_finishes() -> None:
    runner = _FakeRunner()

    image_id = agent_image("python:3.12-bookworm", command_runner=runner, timeout=42)

    assert image_id == _AGENT_IMAGE_ID
    assert runner.verbs() == [
        ("build",),  # the devcontainer CLI image (a layer-cache hit when unchanged)
        ("run",),  # devcontainer build, in that image
        ("build",),  # sets USER and WORKDIR
        ("image", "inspect"),
        ("image", "rm"),  # drops the intermediate tag
    ]
    assert {timeout for _argv, _cwd, timeout in runner.calls} == {42}
    (workspace,) = runner.workspaces
    cli_argv = runner.calls[1][0]
    cli_image = runner.calls[0][0][runner.calls[0][0].index("--tag") + 1]
    root = str(workspace.root)
    assert cli_argv[:4] == ("docker", "run", "--rm", "--volume")
    assert f"{root}:{root}" in cli_argv  # the workspace is mounted at the same path
    assert "/var/run/docker.sock:/var/run/docker.sock" in cli_argv
    assert cli_argv[cli_argv.index(cli_image) + 1] == "build"
    assert workspace.config["image"] == "python:3.12-bookworm"
    assert workspace.config["containerUser"] == "agent"
    assert workspace.feature_ids == {"vibesys-agent"}
    assert workspace.config["features"].keys() == {"./features/vibesys-agent"}


def test_the_finishing_stage_makes_the_image_run_as_agent_in_workspace() -> None:
    """The devcontainer CLI keeps the base's USER and WORKDIR, so a last stage sets both."""
    runner = _FakeRunner()

    agent_image("python:3.12-bookworm", command_runner=runner)

    finish = runner.workspaces[0].finish_dockerfile.splitlines()
    assert finish[-2:] == ["WORKDIR /workspace", "USER agent"]
    cli_argv = runner.calls[1][0]
    features_tag = cli_argv[cli_argv.index("--image-name") + 1]
    final = runner.final_build()
    assert f"BASE_IMAGE={features_tag}" in final
    assert runner.calls[-1][0] == ("docker", "image", "rm", features_tag)
    assert not runner.workspaces[0].root.exists()  # the workspace does not outlive the build


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
    )

    assert image_id == _AGENT_IMAGE_ID
    task_build_argv, task_cwd, _ = runner.calls[0]
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
    assert runner.calls[1][0] == (
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        task_tag,
    )
    assert runner.workspaces[0].config["image"] == task_tag


def test_extra_features_follow_the_agent_feature_in_order(tmp_path: Path) -> None:
    extras = []
    for name in ("zeta", "alpha"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "devcontainer-feature.json").write_text(f'{{"id": "{name}"}}')
        (directory / "install.sh").write_text("#!/bin/sh\n")
        extras.append(LocalFeature(directory))
    runner = _FakeRunner()

    agent_image("python:3.12-bookworm", features=extras, command_runner=runner)

    assert runner.workspaces[0].feature_ids == {"vibesys-agent", "zeta", "alpha"}
    assert list(runner.workspaces[0].config["features"]) == [
        "./features/vibesys-agent",
        "./features/zeta",
        "./features/alpha",
    ]


def test_agent_image_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        agent_image("python:3.12-bookworm", command_runner=_FakeRunner(), timeout=0)


def test_a_failed_devcontainer_build_is_a_task_image_build_error_and_cleans_up() -> None:
    runner = _FakeRunner(cli_returncode=1)

    with pytest.raises(TaskImageBuildError, match=r"devcontainer build failed.*cli failed"):
        agent_image("python:3.12-bookworm", command_runner=runner)

    assert runner.verbs() == [("build",), ("run",)]
    assert not runner.workspaces[0].root.exists()


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
    image = runner.workspaces[0].config["image"]
    assert isinstance(image, str)
    return image


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

        agent_image(base_image, task_dockerfile=task_dockerfile, command_runner=runner)

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

    agent_image(_TASK_IMAGE_ID, command_runner=runner)

    tag = "vibesys-base:" + "a" * 64
    assert runner.calls[0][0] == ("docker", "tag", _TASK_IMAGE_ID, tag)
    assert _devcontainer_base(runner) == tag
