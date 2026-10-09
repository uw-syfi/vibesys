"""A Docker run environment that needs no Docker daemon.

Agents always run in Docker, so a run that does not choose another environment
opens :class:`DockerEnvironment`. Its only external process is the agent image
build, which this spec replaces with an in-memory runner; pair it with a
``FakeComputeBackend`` for the container itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api.request import RunEnvironmentSpec
from vs_agent.api.testing import FakeDockerBuildRunner
from vs_sandbox.api.testing import HostExecutedContainerBackend

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def fake_docker_environment() -> RunEnvironmentSpec:
    """Return a Docker environment spec whose image build is an in-memory fake."""
    return RunEnvironmentSpec("docker", {"build_runner": FakeDockerBuildRunner()})


def host_container_backend(
    _name: object,
    *,
    log_dir: Path,
    log: Callable[[str], None] | None = None,
    image: str | None = None,
) -> HostExecutedContainerBackend:
    """Build the ``backend_factory`` product: a CPU backend with host-executing containers."""
    return HostExecutedContainerBackend(log_dir, log=log, image=image)
