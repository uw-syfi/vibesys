from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from vibesys.sandbox.images import (
    SubprocessDockerBuildRunner,
    TaskImageBuildError,
    build_task_image,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_IMAGE_ID = "sha256:" + "a" * 64


class FakeDockerBuildRunner:
    def __init__(
        self,
        *,
        build_result: subprocess.CompletedProcess[str] | BaseException | None = None,
        inspect_result: subprocess.CompletedProcess[str] | BaseException | None = None,
        image_id: str = _IMAGE_ID,
    ) -> None:
        self.build_result = build_result or subprocess.CompletedProcess(("docker",), 0, "", "")
        self.inspect_result = inspect_result or subprocess.CompletedProcess(
            ("docker",), 0, image_id, ""
        )
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
        result = self.build_result if normalized[1] == "build" else self.inspect_result
        if isinstance(result, BaseException):
            raise result
        return result


def _task(tmp_path: Path) -> Path:
    root = tmp_path / "task"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    return root


def test_build_uses_task_context_and_returns_runnable_image_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_root = _task(tmp_path)
    runner = FakeDockerBuildRunner()
    monkeypatch.setattr("vibesys.sandbox.images.uuid.uuid4", lambda: MagicMock(hex="build-id"))

    assert (
        build_task_image(task_root / "Dockerfile", command_runner=runner, timeout=42) == _IMAGE_ID
    )

    build_argv, cwd, timeout = runner.calls[0]
    assert build_argv == (
        "docker",
        "build",
        "--provenance=false",
        "--tag",
        "vibesys-task-build:build-id",
        "--file",
        str(task_root / "Dockerfile"),
        str(task_root),
    )
    assert cwd == task_root
    assert timeout == 42
    assert runner.calls[1] == (
        (
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            "vibesys-task-build:build-id",
        ),
        task_root,
        42,
    )


def test_build_passes_base_image_as_build_arg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_root = _task(tmp_path)
    runner = FakeDockerBuildRunner()
    monkeypatch.setattr("vibesys.sandbox.images.uuid.uuid4", lambda: MagicMock(hex="build-id"))

    build_task_image(
        task_root / "Dockerfile", base_image="python:3.12-bookworm", command_runner=runner
    )

    build_argv, _cwd, _timeout = runner.calls[0]
    assert build_argv == (
        "docker",
        "build",
        "--provenance=false",
        "--tag",
        "vibesys-task-build:build-id",
        "--file",
        str(task_root / "Dockerfile"),
        "--build-arg",
        "BASE_IMAGE=python:3.12-bookworm",
        str(task_root),
    )


def test_subprocess_runner_uses_no_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = MagicMock(return_value=subprocess.CompletedProcess(("docker",), 0, "", ""))
    monkeypatch.setattr("vibesys.sandbox.images.subprocess.run", run)

    SubprocessDockerBuildRunner().run(("docker", "build", "."), cwd=tmp_path, timeout=5)

    assert run.call_args.args == (("docker", "build", "."),)
    assert run.call_args.kwargs == {
        "cwd": tmp_path,
        "capture_output": True,
        "check": False,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 5,
    }


@pytest.mark.parametrize(
    ("failure", "match"),
    [
        (FileNotFoundError(), "Docker was not found while building"),
        (
            subprocess.TimeoutExpired(("docker", "build"), 7),
            "timed out after 7 seconds while building",
        ),
    ],
)
def test_build_translates_process_failures(
    tmp_path: Path, failure: BaseException, match: str
) -> None:
    runner = FakeDockerBuildRunner(build_result=failure)

    with pytest.raises(TaskImageBuildError, match=match):
        build_task_image(_task(tmp_path) / "Dockerfile", command_runner=runner, timeout=7)

    assert len(runner.calls) == 1


def test_build_reports_bounded_docker_failure(tmp_path: Path) -> None:
    runner = FakeDockerBuildRunner(
        build_result=subprocess.CompletedProcess(("docker",), 17, "ignored", "specific failure"),
    )

    with pytest.raises(TaskImageBuildError, match=r"exit 17.*specific failure"):
        build_task_image(_task(tmp_path) / "Dockerfile", command_runner=runner)


@pytest.mark.parametrize(
    ("image_id", "match"),
    [
        ("", "invalid runnable task image ID.*<empty>"),
        ("sha256:not-hex", "invalid runnable task image ID"),
        ("sha256:" + "a" * 63, "invalid runnable task image ID"),
        ("example:latest", "invalid runnable task image ID"),
    ],
)
def test_build_rejects_malformed_runnable_image_id(
    tmp_path: Path, image_id: str, match: str
) -> None:
    runner = FakeDockerBuildRunner(image_id=image_id)

    with pytest.raises(TaskImageBuildError, match=match):
        build_task_image(_task(tmp_path) / "Dockerfile", command_runner=runner)

    assert len(runner.calls) == 2


@pytest.mark.parametrize(
    ("failure", "match"),
    [
        (FileNotFoundError(), "Docker was not found while inspecting"),
        (
            subprocess.TimeoutExpired(("docker", "image", "inspect"), 7),
            "timed out after 7 seconds while inspecting",
        ),
        (
            subprocess.CompletedProcess(("docker",), 17, "ignored", "inspect failure"),
            r"Could not resolve runnable task image.*exit 17.*inspect failure",
        ),
    ],
)
def test_build_translates_inspect_failures(
    tmp_path: Path,
    failure: subprocess.CompletedProcess[str] | BaseException,
    match: str,
) -> None:
    runner = FakeDockerBuildRunner(inspect_result=failure)

    with pytest.raises(TaskImageBuildError, match=match):
        build_task_image(_task(tmp_path) / "Dockerfile", command_runner=runner, timeout=7)

    assert len(runner.calls) == 2


def test_build_validates_dockerfile_before_running_docker(tmp_path: Path) -> None:
    runner = FakeDockerBuildRunner()

    task_root = tmp_path / "task"
    task_root.mkdir()
    with pytest.raises(TaskImageBuildError, match="Dockerfile must be a regular file"):
        build_task_image(task_root / "Dockerfile", command_runner=runner)
    (task_root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    with pytest.raises(ValueError, match="timeout must be positive"):
        build_task_image(task_root / "Dockerfile", command_runner=runner, timeout=0)
    assert runner.calls == []


def test_build_rejects_dockerfile_symlink_without_widening_context(tmp_path: Path) -> None:
    task_root = tmp_path / "task"
    task_root.mkdir()
    outside = tmp_path / "outside.Dockerfile"
    outside.write_text("FROM scratch\n", encoding="utf-8")
    dockerfile = task_root / "Dockerfile"
    dockerfile.symlink_to(outside)
    runner = FakeDockerBuildRunner()

    with pytest.raises(TaskImageBuildError, match="must be a regular file"):
        build_task_image(dockerfile, command_runner=runner)

    assert runner.calls == []
