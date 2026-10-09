"""A fake Docker daemon for run-environment tests whose agent must really run a command.

``DaemonBackend`` hands out real ``DockerSandbox`` objects over a
``FakeDockerEngine``, which runs ``docker exec`` programs locally with the
container's mounts and environment applied. A command run through the sandbox
therefore reaches host sockets the environment bind-mounted into it.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from vibesys.constants import ComputeBackend
from vs_runtime.api.infrastructure import DockerEnvironmentConfig
from vs_sandbox.api import DockerSandbox, SandboxKind
from vs_sandbox.api.testing import FakeDockerEngine

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vs_sandbox.api import CommandRunner, HostResource

_IMAGE_ID = "sha256:" + "d" * 64


class InMemoryBuildRunner:
    """Answers ``docker build`` and ``docker image inspect`` without a daemon."""

    def run(
        self, argv: Sequence[str], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        return subprocess.CompletedProcess(
            tuple(argv), 0, _IMAGE_ID if argv[1] == "image" else "", ""
        )


class DaemonBackend:
    """A compute backend whose Docker sandboxes talk to a :class:`FakeDockerEngine`."""

    image = "base-image"
    name = ComputeBackend.CUDA

    def __init__(self, engine: FakeDockerEngine) -> None:
        self.engine = engine
        self.attach_accelerator: list[bool] = []
        self.kinds: list[SandboxKind] = []

    def make_sandbox(  # noqa: PLR0913  # lint-waiver: LW-954390 [PLR0913]; the method mirrors the backend construction contract it fakes.
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        docker_in_docker: bool = False,
        container_image: str | None = None,
        resources: Sequence[HostResource] = (),
        same_path_workspace: bool = False,
        extra_env: dict[str, str] | None = None,
        attach_accelerator: bool = True,
        **_other: object,
    ) -> CommandRunner:
        self.kinds.append(kind)
        self.attach_accelerator.append(attach_accelerator)
        return DockerSandbox(
            host_workspace=host_workspace,
            image=container_image or self.image,
            resources=resources,
            docker=self.engine,
            docker_in_docker=docker_in_docker,
            same_path_workspace=same_path_workspace,
            env=extra_env,
        )

    def make_monitor(self, log_dir: Path) -> None:
        del log_dir

    def reselect_device(self) -> None:
        return


def daemon_engine(directory: Path) -> FakeDockerEngine:
    """Return a fake daemon keeping its bookkeeping under *directory*."""
    state = directory / "engine"
    state.mkdir(exist_ok=True)
    return FakeDockerEngine(
        state, agent_ids=(os.getuid(), os.getgid()), runtimes=("runc", "sysbox-runc")
    )


def daemon_docker_config(engine: FakeDockerEngine) -> DockerEnvironmentConfig:
    """Return the Docker settings that send an environment's containers to *engine*."""
    return DockerEnvironmentConfig(docker=engine, build_runner=InMemoryBuildRunner())
