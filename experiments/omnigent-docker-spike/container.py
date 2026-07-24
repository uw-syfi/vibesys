"""VibeSys-owned Docker container lifecycle — zero Omnigent dependency.

This is the substance of the "isolate hard" design. All container behavior —
create with GPU/device/mount args, exec, copy in, stream, remove — lives here,
owned by VibeSys, and is a faithful (spike-scale) restatement of what
``libs/vs-sandbox/src/vs_sandbox/docker_sandbox.py`` already does. The Omnigent
adapter in ``omnigent_launcher.py`` delegates to this protocol and adds no
lifecycle logic of its own, so if Omnigent's ``SandboxLauncher`` ABC changes,
only the thin adapter moves; this file does not.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from spec import VibesysSandboxSpec


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


class ContainerProcess(Protocol):
    """A running streamed command."""

    def lines(self) -> Iterator[str]: ...
    def wait(self) -> int: ...
    def close(self) -> None: ...


class ContainerSandbox(Protocol):
    """VibeSys's container contract. Omnigent never sees this type."""

    def create(self, spec: VibesysSandboxSpec, *, name: str) -> str: ...
    def exec(self, cid: str, command: str, *, check: bool = True) -> ExecResult: ...
    def exec_background(self, cid: str, command: str) -> None: ...
    def copy_in(self, cid: str, local: Path, remote: str) -> None: ...
    def stream(self, cid: str, command: str) -> ContainerProcess: ...
    def is_running(self, cid: str) -> bool: ...
    def remove(self, cid: str) -> None: ...


class _DockerStream:
    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc

    def lines(self) -> Iterator[str]:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            yield line.rstrip("\n")

    def wait(self) -> int:
        return self._proc.wait()

    def close(self) -> None:
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if self._proc.poll() is None:
            self._proc.terminate()


class DockerContainerSandbox:
    """Local Docker implementation of :class:`ContainerSandbox`.

    Deliberately small: it demonstrates that VibeSys can own the exact resource
    knobs (GPU, devices, bind mounts) that Omnigent's provider boundary cannot
    express, using nothing but the Docker CLI.
    """

    def __init__(self, *, docker: str = "docker") -> None:
        self._docker = docker

    def create(self, spec: VibesysSandboxSpec, *, name: str) -> str:
        # The unique suffix keeps concurrent VibeSys runs from colliding, the
        # same reason ``vs_sandbox`` names containers per run.
        container_name = f"{name}-{uuid.uuid4().hex[:8]}"
        argv = spec.docker_run_argv(name=container_name)
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"docker run failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def exec(self, cid: str, command: str, *, check: bool = True) -> ExecResult:
        result = subprocess.run(
            [self._docker, "exec", cid, "sh", "-lc", command],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"docker exec failed ({result.returncode}): {result.stderr.strip()}"
            )
        return ExecResult(result.returncode, result.stdout, result.stderr)

    def exec_background(self, cid: str, command: str) -> None:
        result = subprocess.run(
            [self._docker, "exec", "-d", cid, "sh", "-lc", command],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker exec -d failed: {result.stderr.strip()}")

    def copy_in(self, cid: str, local: Path, remote: str) -> None:
        result = subprocess.run(
            [self._docker, "cp", str(local), f"{cid}:{remote}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker cp failed: {result.stderr.strip()}")

    def stream(self, cid: str, command: str) -> ContainerProcess:
        proc = subprocess.Popen(
            [self._docker, "exec", cid, "sh", "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        return _DockerStream(proc)

    def is_running(self, cid: str) -> bool:
        result = subprocess.run(
            [self._docker, "inspect", "-f", "{{.State.Running}}", cid],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def remove(self, cid: str) -> None:
        # Idempotent force-remove, matching the cleanup guarantee vs-sandbox
        # added in PR #228 (own the container until ``docker rm -f`` confirms).
        subprocess.run(
            [self._docker, "rm", "-f", cid],
            capture_output=True,
            text=True,
            check=False,
        )
