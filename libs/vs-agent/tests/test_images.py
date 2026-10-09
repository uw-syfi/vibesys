"""Tests for the agent image build (:func:`vs_agent.api.images.agent_image`).

``build_task_image`` itself is covered by ``test_task_image.py``. This file
covers the agent layer: its build args, how
it chains onto a task image, and that the Dockerfile it builds ships with the
package.
"""

from __future__ import annotations

import importlib.resources
import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import (
    CLI_VERSIONS,
    CONTAINER_RUNTIME_TOOLCHAIN,
    DOCKER_COMPOSE_VERSION,
    DOCKER_ENGINE_VERSION,
    GO_TOOLCHAIN_VERSION,
    KIND_VERSION,
    KUBECTL_VERSION,
    NODE_VERSION,
    RUST_TOOLCHAIN_VERSION,
)
from vs_agent.api.images import agent_image

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

_AGENT_IMAGE_DIR = Path(str(importlib.resources.files("vs_agent").joinpath("images")))
_AGENT_DOCKERFILE = _AGENT_IMAGE_DIR / "agent.Dockerfile"

_TASK_IMAGE_ID = "sha256:" + "a" * 64
_AGENT_IMAGE_ID = "sha256:" + "b" * 64


class _FakeRunner:
    """Distinguishes a task-image build/inspect pair from an agent-image one
    by the repository prefix of the tag being built or inspected, so a test
    can chain the two builds the way ``agent_image`` really does."""

    def __init__(
        self,
        *,
        task_image_id: str = _TASK_IMAGE_ID,
        agent_image_id: str = _AGENT_IMAGE_ID,
    ) -> None:
        self.task_image_id = task_image_id
        self.agent_image_id = agent_image_id
        self.calls: list[tuple[tuple[str, ...], Path, float]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        normalized = tuple(argv)
        self.calls.append((normalized, cwd, timeout))
        if normalized[1] == "build":
            return subprocess.CompletedProcess(("docker",), 0, "", "")
        tag = normalized[-1]
        image_id = (
            self.task_image_id if tag.startswith("vibesys-task-build:") else self.agent_image_id
        )
        return subprocess.CompletedProcess(("docker",), 0, image_id, "")


def _expected_version_args() -> tuple[str, ...]:
    """The NODE_VERSION/CLI_VERSIONS build args ``agent_image`` should emit,
    read from :mod:`vs_agent.api` at test time (the same module the builder
    reads), so this asserts real plumbing rather than a patched round-trip."""
    args: list[str] = ["--build-arg", f"NODE_VERSION={NODE_VERSION}"]
    for provider, version in CLI_VERSIONS.items():
        args += ["--build-arg", f"{provider.upper()}_VERSION={version}"]
    return tuple(args)


def _expected_container_runtime_args() -> tuple[str, ...]:
    """The container-runtime build args, read from :mod:`vs_agent.api` like the builder does."""
    return (
        "--build-arg",
        f"DOCKER_ENGINE_VERSION={DOCKER_ENGINE_VERSION}",
        "--build-arg",
        f"DOCKER_COMPOSE_VERSION={DOCKER_COMPOSE_VERSION}",
        "--build-arg",
        f"KIND_VERSION={KIND_VERSION}",
        "--build-arg",
        f"KUBECTL_VERSION={KUBECTL_VERSION}",
    )


def test_agent_image_base_only_argv() -> None:
    """No task Dockerfile: one build, directly on the base image."""
    runner = _FakeRunner()

    image_id = agent_image("python:3.12-bookworm", command_runner=runner, timeout=42)

    assert image_id == _AGENT_IMAGE_ID
    assert len(runner.calls) == 2
    build_argv, cwd, timeout = runner.calls[0]
    tag = build_argv[4]
    assert tag.startswith("vibesys-agent-build:")
    assert build_argv == (
        "docker",
        "build",
        "--provenance=false",
        "--tag",
        tag,
        "--file",
        str(_AGENT_DOCKERFILE),
        "--build-arg",
        "BASE_IMAGE=python:3.12-bookworm",
        *_expected_version_args(),
        "--build-arg",
        "TOOLCHAINS=",
        "--build-arg",
        f"RUST_VERSION={RUST_TOOLCHAIN_VERSION}",
        "--build-arg",
        f"GO_VERSION={GO_TOOLCHAIN_VERSION}",
        *_expected_container_runtime_args(),
        "--build-arg",
        "PIP_EXTRAS=",
        str(_AGENT_IMAGE_DIR),
    )
    assert cwd == _AGENT_IMAGE_DIR
    assert timeout == 42
    assert runner.calls[1] == (
        ("docker", "image", "inspect", "--format", "{{.Id}}", tag),
        _AGENT_IMAGE_DIR,
        42,
    )


def test_agent_image_chains_task_image_as_base(tmp_path: Path) -> None:
    """A task Dockerfile is built first; its unique local tag becomes BASE_IMAGE
    for the agent layer (BuildKit cannot resolve a bare image ID), and BASE_IMAGE is also forwarded into the task build so
    a task Dockerfile may declare ``ARG BASE_IMAGE`` / ``FROM ${BASE_IMAGE}``.
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
    assert len(runner.calls) == 4

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

    task_inspect_argv, _, _ = runner.calls[1]
    assert task_inspect_argv == (
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        task_tag,
    )

    agent_build_argv, agent_cwd, _ = runner.calls[2]
    agent_tag = agent_build_argv[4]
    assert agent_tag.startswith("vibesys-agent-build:")
    assert agent_build_argv == (
        "docker",
        "build",
        "--provenance=false",
        "--tag",
        agent_tag,
        "--file",
        str(_AGENT_DOCKERFILE),
        "--build-arg",
        f"BASE_IMAGE={task_tag}",
        *_expected_version_args(),
        "--build-arg",
        "TOOLCHAINS=",
        "--build-arg",
        f"RUST_VERSION={RUST_TOOLCHAIN_VERSION}",
        "--build-arg",
        f"GO_VERSION={GO_TOOLCHAIN_VERSION}",
        *_expected_container_runtime_args(),
        "--build-arg",
        "PIP_EXTRAS=",
        str(_AGENT_IMAGE_DIR),
    )
    assert agent_cwd == _AGENT_IMAGE_DIR


@pytest.mark.parametrize(
    ("toolchains", "expected"),
    [
        (("go", "rust"), "go rust"),
        (("rust", "go"), "go rust"),
        (["rust", "rust", "go"], "go rust"),
        ({"go"}, "go"),
        ((), ""),
    ],
)
def test_agent_image_sorts_and_dedupes_toolchains(
    toolchains: Collection[str],
    expected: str,
) -> None:
    runner = _FakeRunner()

    agent_image("python:3.12-bookworm", toolchains=toolchains, command_runner=runner)

    build_argv, _, _ = runner.calls[0]
    toolchains_arg = next(arg for arg in build_argv if arg.startswith("TOOLCHAINS="))
    assert toolchains_arg == f"TOOLCHAINS={expected}"


def test_agent_image_versions_come_from_the_agent_api() -> None:
    runner = _FakeRunner()

    agent_image("python:3.12-bookworm", command_runner=runner)

    build_argv, _, _ = runner.calls[0]
    assert "--build-arg" in build_argv
    assert f"NODE_VERSION={NODE_VERSION}" in build_argv
    for provider, version in CLI_VERSIONS.items():
        assert f"{provider.upper()}_VERSION={version}" in build_argv
    assert f"RUST_VERSION={RUST_TOOLCHAIN_VERSION}" in build_argv
    assert f"GO_VERSION={GO_TOOLCHAIN_VERSION}" in build_argv


def test_agent_image_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        agent_image("python:3.12-bookworm", command_runner=_FakeRunner(), timeout=0)


def test_agent_dockerfile_ships_as_package_data() -> None:
    """The build context directory (and Dockerfile) resolve through
    ``importlib.resources`` against the installed package, not just as a
    source-tree-relative path, so this catches a wheel that forgot to declare
    the data in ``[tool.setuptools.package-data]``.
    """
    dockerfile = importlib.resources.files("vs_agent").joinpath("images", "agent.Dockerfile")
    assert dockerfile.is_file()
    assert "USER agent" in dockerfile.read_text(encoding="utf-8")


def test_agent_dockerfile_has_one_arg_per_cli_version() -> None:
    text = _AGENT_DOCKERFILE.read_text(encoding="utf-8")
    declared_args = set(re.findall(r"(?m)^ARG\s+([A-Z0-9_]+)", text))
    expected_args = {f"{provider.upper()}_VERSION" for provider in CLI_VERSIONS}
    assert expected_args <= declared_args


def test_pip_extras_are_rendered_sorted_and_deduplicated() -> None:
    runner = _FakeRunner()

    agent_image(
        "python:3.12-bookworm",
        pip_extras=("modal>=0.66", "b", "modal>=0.66"),
        command_runner=runner,
    )

    build_argv, _cwd, _timeout = runner.calls[0]
    assert "PIP_EXTRAS=b modal>=0.66" in build_argv


def test_container_runtime_toolchain_is_opt_in_and_pinned_outside_the_dockerfile() -> None:
    """The container-runtime layer is selected by toolchain name, never by default,
    and its versions arrive as build args from the agent API like every other pin."""
    ordinary = _FakeRunner()
    agent_image("python:3.12-bookworm", command_runner=ordinary)
    topology = _FakeRunner()
    agent_image(
        "python:3.12-bookworm",
        toolchains={CONTAINER_RUNTIME_TOOLCHAIN, "go"},
        command_runner=topology,
    )

    ordinary_argv = ordinary.calls[0][0]
    topology_argv = topology.calls[0][0]
    assert "TOOLCHAINS=" in ordinary_argv
    assert "TOOLCHAINS=container-runtime go" in topology_argv
    assert set(_expected_container_runtime_args()) <= set(topology_argv)
    dockerfile = _AGENT_DOCKERFILE.read_text(encoding="utf-8")
    assert f'*" {CONTAINER_RUNTIME_TOOLCHAIN} "*' in dockerfile
    for version in (DOCKER_ENGINE_VERSION, DOCKER_COMPOSE_VERSION, KIND_VERSION, KUBECTL_VERSION):
        assert version not in dockerfile


_BARE_ID = re.compile(r"sha256:[0-9a-f]{64}")


def _base_image_arg(build_argv: Sequence[str]) -> str:
    return next(
        arg.removeprefix("BASE_IMAGE=") for arg in build_argv if arg.startswith("BASE_IMAGE=")
    )


def _agent_build_argv(runner: _FakeRunner) -> Sequence[str]:
    return next(
        argv
        for argv, _cwd, _timeout in runner.calls
        if argv[1] == "build" and any(a.startswith("vibesys-agent-build:") for a in argv)
    )


@given(
    base_image=st.one_of(
        st.from_regex(r"sha256:[0-9a-f]{64}", fullmatch=True),
        st.sampled_from(["python:3.12-bookworm", "ubuntu:24.04", "ghcr.io/x/y@sha256:" + "c" * 64]),
    ),
    flow=st.sampled_from(["base-only", "task-dockerfile"]),
)
def test_agent_base_image_is_never_a_bare_image_id(base_image: str, flow: str) -> None:
    """BuildKit resolves ``FROM sha256:<id>`` as a registry reference and fails
    (#1520), so the agent layer's BASE_IMAGE is a tag for every input."""
    with tempfile.TemporaryDirectory() as tmp:
        task_dockerfile = None
        if flow == "task-dockerfile":
            task_dockerfile = Path(tmp) / "Dockerfile"
            task_dockerfile.write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n", encoding="utf-8")
        runner = _FakeRunner()

        agent_image(base_image, task_dockerfile=task_dockerfile, command_runner=runner)

        for argv, _cwd, _timeout in runner.calls:
            if argv[1] == "build":
                assert _BARE_ID.fullmatch(_base_image_arg(argv)) is None
        agent_base = _base_image_arg(_agent_build_argv(runner))
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
    assert _base_image_arg(runner.calls[1][0]) == tag
