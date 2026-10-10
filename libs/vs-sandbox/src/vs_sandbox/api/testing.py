"""The owned fake/test-double surface for ``vs_sandbox``.

Tests import doubles from here rather than reaching into the library's
internal modules directly.
"""

from __future__ import annotations

from vs_sandbox.fake_command_runner import (
    DEFAULT_RESULT,
    FakeCommandRunner,
    FakeExecution,
    FakeLifecycleRunner,
)
from vs_sandbox.fake_compute_backend import (
    FakeAcceleratorDiscovery,
    FakeComputeBackend,
    FakeRunnerCreation,
)
from vs_sandbox.fake_docker_command import (
    DockerCliCall,
    DockerCliOutcome,
    DockerCommandCall,
    DockerCommandOutcome,
    FakeDockerCommandRunner,
    ScriptedDockerCli,
    docker_missing,
    docker_result,
    docker_timed_out,
)
from vs_sandbox.fake_docker_engine import FakeContainer, FakeDockerEngine
from vs_sandbox.fake_host_container import HostExecutedContainer, HostExecutedContainerBackend

__all__ = [
    "DEFAULT_RESULT",
    "DockerCliCall",
    "DockerCliOutcome",
    "DockerCommandCall",
    "DockerCommandOutcome",
    "FakeAcceleratorDiscovery",
    "FakeCommandRunner",
    "FakeComputeBackend",
    "FakeContainer",
    "FakeDockerCommandRunner",
    "FakeDockerEngine",
    "FakeExecution",
    "FakeLifecycleRunner",
    "FakeRunnerCreation",
    "HostExecutedContainer",
    "HostExecutedContainerBackend",
    "ScriptedDockerCli",
    "docker_missing",
    "docker_result",
    "docker_timed_out",
]
