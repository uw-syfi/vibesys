"""In-memory :class:`~vs_agent.images.DockerBuildRunner` that needs no Docker daemon."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_agent.images import (
    DOCKER_PROGRESS_LINE_CHARACTERS,
    DockerBuildResult,
    DockerCommandResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

DEFAULT_IMAGE_ID = "sha256:" + "a" * 64

type DockerResult = subprocess.CompletedProcess[str]
"""Legacy subprocess result used by Docker CLI fakes outside image building."""


class FakeDockerBuildRunner:
    """Answer image builds and ``docker image inspect`` from scripted results.

    A build succeeds and inspection reports ``image_id`` unless a result (or an
    exception to raise) is scripted. Every call is recorded in ``calls``. Like
    the real runner, a command streams each line of its result's ``stderr``
    to ``on_stderr_line`` before returning; a script's build ``stderr`` is
    where Docker's progress output goes.
    """

    def __init__(
        self,
        *,
        build_result: DockerBuildResult | BaseException | None = None,
        inspect_result: DockerBuildResult | BaseException | None = None,
        image_id: str = DEFAULT_IMAGE_ID,
    ) -> None:
        """Script the build and inspect outcomes."""
        self.build_result = build_result or docker_build_result()
        self.inspect_result = inspect_result or docker_build_result(stdout=image_id)
        self.calls: list[tuple[tuple[str, ...], Path, float]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        on_stderr_line: Callable[[str], None] | None = None,
    ) -> DockerBuildResult:
        """Record the command and return the scripted outcome for its kind."""
        normalized = tuple(argv)
        self.calls.append((normalized, cwd, timeout))
        result = self.build_result if normalized[1] in ("build", "run") else self.inspect_result
        if isinstance(result, BaseException):
            raise result
        if on_stderr_line is not None:
            for line in (result.stderr or "").splitlines():
                on_stderr_line(line[:DOCKER_PROGRESS_LINE_CHARACTERS])
        return result


def docker_result(
    args: Sequence[str] = ("docker",), returncode: int = 0, stdout: str = "", stderr: str = ""
) -> DockerResult:
    """Build an ordinary subprocess result for a Docker CLI Fake."""
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


@dataclass(frozen=True)
class _UncapturedDockerResult:
    """Legacy command result from a runner configured without stream capture."""

    args: tuple[str, ...]
    returncode: int
    stdout: None = None
    stderr: None = None


def docker_result_without_output(
    args: Sequence[str] = ("docker",), returncode: int = 0
) -> DockerBuildResult:
    """Build a legacy result from a runner that did not capture either stream."""
    return _UncapturedDockerResult(tuple(args), returncode)


def docker_build_result(
    args: Sequence[str] = ("docker",),
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> DockerBuildResult:
    """Build an untruncated result for a Docker image-build Fake."""
    return DockerCommandResult(
        tuple(args),
        returncode,
        stdout,
        stderr,
    )


def docker_timed_out(args: Sequence[str], timeout_seconds: float) -> subprocess.TimeoutExpired:
    """Build the error a Docker command raises when it outlives its timeout."""
    return subprocess.TimeoutExpired(args, timeout_seconds)
