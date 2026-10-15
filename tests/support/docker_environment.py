"""A Docker run environment that needs no Docker daemon.

Agents always run in Docker, so a run that does not choose another environment
opens :class:`DockerEnvironment`. This spec replaces image builds, Docker daemon
calls, and orphan-process confinement with in-memory fakes; pair it with a
``FakeComputeBackend`` for the run's own container.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from vibesys.api.request import RunEnvironmentSpec
from vs_agent.api import reap_orphaned_agents
from vs_agent.api.testing import FakeConfinement, FakeDockerBuildRunner
from vs_sandbox.api.testing import FakeDockerEngine, HostExecutedContainerBackend
from vs_sim.api import OsThreads, SubprocessPipedProcessLauncher
from vs_sim.api.testing import FakePipedProcessLauncher

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_sim.api import Clock


def fake_docker_environment(docker: FakeDockerEngine | None = None) -> RunEnvironmentSpec:
    """Return a Docker environment spec whose host effects are in-memory fakes."""
    engine = docker if docker is not None else FakeDockerEngine(launcher=FakePipedProcessLauncher())
    return RunEnvironmentSpec(
        "docker",
        {
            "build_runner": FakeDockerBuildRunner(),
            "docker": engine,
            "orphan_reaper": partial(
                reap_orphaned_agents,
                confinement_for=lambda _container_id: FakeConfinement(),
            ),
        },
    )


def host_container_backend(
    _name: object,
    *,
    log_dir: Path,
    log: Callable[[str], None] | None = None,
    image: str | None = None,
    clock: Clock,
    events: object | None = None,
) -> HostExecutedContainerBackend:
    """Build the ``backend_factory`` product: a CPU backend with host-executing containers.

    Its commands run as real host processes, so it takes the real launcher and threads.
    """
    del clock, events  # the fake backend records no timestamps and has no device to watch
    return HostExecutedContainerBackend(
        log_dir,
        log=log,
        image=image,
        threads=OsThreads(),
        launcher=SubprocessPipedProcessLauncher(),
    )
