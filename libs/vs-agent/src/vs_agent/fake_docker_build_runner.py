"""In-memory :class:`~vs_agent.images.DockerBuildRunner` that needs no Docker daemon."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

DEFAULT_IMAGE_ID = "sha256:" + "a" * 64

type DockerResult = subprocess.CompletedProcess[str]
"""What a Docker command returns: its argv, exit status, and captured output."""


class FakeDockerBuildRunner:
    """Answer ``docker build`` and ``docker image inspect`` from scripted results.

    A build succeeds and inspection reports ``image_id`` unless a result (or an
    exception to raise) is scripted. Every call is recorded in ``calls``.
    """

    def __init__(
        self,
        *,
        build_result: DockerResult | BaseException | None = None,
        inspect_result: DockerResult | BaseException | None = None,
        image_id: str = DEFAULT_IMAGE_ID,
    ) -> None:
        """Script the build and inspect outcomes."""
        self.build_result = build_result or docker_result()
        self.inspect_result = inspect_result or docker_result(stdout=image_id)
        self.calls: list[tuple[tuple[str, ...], Path, float]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> DockerResult:
        """Record the command and return the scripted outcome for its kind."""
        normalized = tuple(argv)
        self.calls.append((normalized, cwd, timeout))
        result = self.build_result if normalized[1] == "build" else self.inspect_result
        if isinstance(result, BaseException):
            raise result
        return result


def docker_result(
    args: Sequence[str] = ("docker",), returncode: int = 0, stdout: str = "", stderr: str = ""
) -> DockerResult:
    """Build the result of a finished Docker command, for a Fake to return."""
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def docker_timed_out(args: Sequence[str], timeout_seconds: float) -> subprocess.TimeoutExpired:
    """Build the error a Docker command raises when it outlives its timeout."""
    return subprocess.TimeoutExpired(args, timeout_seconds)
