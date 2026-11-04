"""In-memory :class:`~vs_agent.images.DockerBuildRunner` that needs no Docker daemon."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_agent.images import DockerBuildResult, DockerCommandResult
from vs_sim.api import OutputStream

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from vs_sim.api import OutputSink

DEFAULT_IMAGE_ID = "sha256:" + "a" * 64

type DockerResult = subprocess.CompletedProcess[str]
"""Legacy subprocess result used by Docker CLI fakes outside image building."""


class FakeDockerBuildRunner:
    """Answer image builds and ``docker image inspect`` from scripted results.

    It models the daemon's image store: a build that succeeds adds its
    ``--tag`` to ``images``, and inspecting a name absent from ``images``
    fails like Docker's "No such image". Inspecting a name in ``images``
    reports ``image_id`` unless a result (or an exception to raise) is
    scripted. A build succeeds unless one is scripted. Every call is recorded
    in ``calls``. Like
    the real runner, a command writes its result's ``stdout`` and ``stderr``
    to ``output`` before returning; a script's build ``stderr`` is where
    Docker's progress output goes.
    """

    def __init__(
        self,
        *,
        build_result: DockerBuildResult | BaseException | None = None,
        inspect_result: DockerBuildResult | BaseException | None = None,
        image_id: str = DEFAULT_IMAGE_ID,
        images: Iterable[str] = (),
    ) -> None:
        """Script the build and inspect outcomes; *images* are the names the daemon already has."""
        self.images: set[str] = set(images)
        self.build_result = build_result or docker_build_result()
        self.inspect_result = inspect_result or docker_build_result(stdout=image_id)
        self.calls: list[tuple[tuple[str, ...], Path, float]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        output: OutputSink,
    ) -> DockerBuildResult:
        """Record the command and return the scripted outcome for its kind."""
        normalized = tuple(argv)
        self.calls.append((normalized, cwd, timeout))
        if normalized[1:3] == ("image", "inspect") and normalized[-1] not in self.images:
            absent = docker_build_result(
                normalized, 1, "", f"Error response from daemon: No such image: {normalized[-1]}"
            )
            output.write(OutputStream.STDERR, (absent.stderr or "").encode())
            return absent
        result = self.build_result if normalized[1] in ("build", "run") else self.inspect_result
        built = normalized[1] == "build" and not isinstance(result, BaseException)
        if built and result.returncode == 0 and "--tag" in normalized:
            self.images.add(normalized[normalized.index("--tag") + 1])
        if isinstance(result, BaseException):
            raise result
        output.write(OutputStream.STDOUT, (result.stdout or "").encode())
        output.write(OutputStream.STDERR, (result.stderr or "").encode())
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
