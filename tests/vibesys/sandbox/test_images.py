"""Tests for the agent image build (:func:`vibesys.sandbox.images.agent_image`).

``build_task_image`` itself is covered by ``test_task_image.py`` (the module
it now lives in moved to :mod:`vibesys.sandbox.images`, but its behavior and
tests did not change). This file covers the agent layer: its build args, how
it chains onto a task image, and that the Dockerfile it builds ships with the
package.
"""

from __future__ import annotations

import importlib.resources
import re
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from vibesys.sandbox.images import _AGENT_DOCKERFILE, _AGENT_IMAGE_DIR, agent_image
from vs_agent import api as agent_api

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from pathlib import Path

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


def _patch_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vibesys.sandbox.images.uuid.uuid4", lambda: MagicMock(hex="build-id"))


def _expected_version_args() -> tuple[str, ...]:
    """The NODE_VERSION/CLI_VERSIONS build args ``agent_image`` should emit,
    read from :mod:`vs_agent.api` at test time (the same module the builder
    reads), so this asserts real plumbing rather than a patched round-trip."""
    args: list[str] = ["--build-arg", f"NODE_VERSION={agent_api.NODE_VERSION}"]
    for provider, version in agent_api.CLI_VERSIONS.items():
        args += ["--build-arg", f"{provider.upper()}_VERSION={version}"]
    return tuple(args)


def test_agent_image_base_only_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """No task Dockerfile: one build, directly on the base image."""
    _patch_uuid(monkeypatch)
    runner = _FakeRunner()

    image_id = agent_image("python:3.12-bookworm", command_runner=runner, timeout=42)

    assert image_id == _AGENT_IMAGE_ID
    assert len(runner.calls) == 2
    build_argv, cwd, timeout = runner.calls[0]
    assert build_argv == (
        "docker",
        "build",
        "--provenance=false",
        "--tag",
        "vibesys-agent-build:build-id",
        "--file",
        str(_AGENT_DOCKERFILE),
        "--build-arg",
        "BASE_IMAGE=python:3.12-bookworm",
        *_expected_version_args(),
        "--build-arg",
        "TOOLCHAINS=",
        "--build-arg",
        f"RUST_VERSION={agent_api.RUST_TOOLCHAIN_VERSION}",
        "--build-arg",
        f"GO_VERSION={agent_api.GO_TOOLCHAIN_VERSION}",
        "--build-arg",
        "PIP_EXTRAS=",
        str(_AGENT_IMAGE_DIR),
    )
    assert cwd == _AGENT_IMAGE_DIR
    assert timeout == 42
    assert runner.calls[1] == (
        ("docker", "image", "inspect", "--format", "{{.Id}}", "vibesys-agent-build:build-id"),
        _AGENT_IMAGE_DIR,
        42,
    )


def test_agent_image_chains_task_image_as_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task Dockerfile is built first; its image ID becomes BASE_IMAGE for
    the agent layer, and BASE_IMAGE is also forwarded into the task build so
    a task Dockerfile may declare ``ARG BASE_IMAGE`` / ``FROM ${BASE_IMAGE}``.
    """
    _patch_uuid(monkeypatch)
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
    assert task_build_argv == (
        "docker",
        "build",
        "--provenance=false",
        "--tag",
        "vibesys-task-build:build-id",
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
        "vibesys-task-build:build-id",
    )

    agent_build_argv, agent_cwd, _ = runner.calls[2]
    assert agent_build_argv == (
        "docker",
        "build",
        "--provenance=false",
        "--tag",
        "vibesys-agent-build:build-id",
        "--file",
        str(_AGENT_DOCKERFILE),
        "--build-arg",
        f"BASE_IMAGE={_TASK_IMAGE_ID}",
        *_expected_version_args(),
        "--build-arg",
        "TOOLCHAINS=",
        "--build-arg",
        f"RUST_VERSION={agent_api.RUST_TOOLCHAIN_VERSION}",
        "--build-arg",
        f"GO_VERSION={agent_api.GO_TOOLCHAIN_VERSION}",
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
    monkeypatch: pytest.MonkeyPatch,
    toolchains: Collection[str],
    expected: str,
) -> None:
    _patch_uuid(monkeypatch)
    runner = _FakeRunner()

    agent_image("python:3.12-bookworm", toolchains=toolchains, command_runner=runner)

    build_argv, _, _ = runner.calls[0]
    toolchains_arg = next(arg for arg in build_argv if arg.startswith("TOOLCHAINS="))
    assert toolchains_arg == f"TOOLCHAINS={expected}"


def test_agent_image_versions_come_from_the_agent_api(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_uuid(monkeypatch)
    monkeypatch.setattr(agent_api, "NODE_VERSION", "1.2.3")
    monkeypatch.setattr(agent_api, "CLI_VERSIONS", {"claude": "9.9.9", "codex": "8.8.8"})
    monkeypatch.setattr(agent_api, "RUST_TOOLCHAIN_VERSION", "1.0.0")
    monkeypatch.setattr(agent_api, "GO_TOOLCHAIN_VERSION", "1.0.1")
    runner = _FakeRunner()

    agent_image("python:3.12-bookworm", command_runner=runner)

    build_argv, _, _ = runner.calls[0]
    assert "--build-arg" in build_argv
    assert "NODE_VERSION=1.2.3" in build_argv
    assert "CLAUDE_VERSION=9.9.9" in build_argv
    assert "CODEX_VERSION=8.8.8" in build_argv
    assert "RUST_VERSION=1.0.0" in build_argv
    assert "GO_VERSION=1.0.1" in build_argv
    # Only the providers CLI_VERSIONS names should appear; no leftover
    # GEMINI/OPENCODE args from a stale patch in another test.
    assert not any(arg.startswith("GEMINI_VERSION=") for arg in build_argv)


def test_agent_image_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        agent_image("python:3.12-bookworm", command_runner=_FakeRunner(), timeout=0)


def test_agent_dockerfile_ships_as_package_data() -> None:
    """The build context directory (and Dockerfile) resolve through
    ``importlib.resources`` against the installed package, not just as a
    source-tree-relative path, so this catches a wheel that forgot to declare
    the data in ``[tool.setuptools.package-data]``.
    """
    dockerfile = importlib.resources.files("vibesys.sandbox").joinpath("images", "agent.Dockerfile")
    assert dockerfile.is_file()
    assert "USER agent" in dockerfile.read_text(encoding="utf-8")


def test_agent_dockerfile_has_one_arg_per_cli_version() -> None:
    text = _AGENT_DOCKERFILE.read_text(encoding="utf-8")
    declared_args = set(re.findall(r"(?m)^ARG\s+([A-Z0-9_]+)", text))
    expected_args = {f"{provider.upper()}_VERSION" for provider in agent_api.CLI_VERSIONS}
    assert expected_args <= declared_args


def test_pip_extras_are_rendered_sorted_and_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_uuid(monkeypatch)
    runner = _FakeRunner()

    agent_image(
        "python:3.12-bookworm",
        pip_extras=("modal>=0.66", "b", "modal>=0.66"),
        command_runner=runner,
    )

    build_argv, _cwd, _timeout = runner.calls[0]
    assert "PIP_EXTRAS=b modal>=0.66" in build_argv
