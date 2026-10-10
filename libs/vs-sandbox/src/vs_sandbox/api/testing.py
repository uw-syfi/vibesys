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
from vs_sandbox.fake_gpu_telemetry import FakeGpuTelemetry
from vs_sandbox.fake_host_container import HostExecutedContainer, HostExecutedContainerBackend
from vs_sandbox.fake_signal_relay import FakeSignalRelay
from vs_sandbox.fake_stoppable_process import FakeStoppableProcess, StoppableScript
from vs_sandbox.gpu_telemetry_contracts import GpuTelemetryContract, TelemetryHarness
from vs_sandbox.process_contracts import ProcessHarness, StoppableProcessContract
from vs_sandbox.signal_relay_contracts import RelayUnderTest, SignalRelayContract

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
    "FakeGpuTelemetry",
    "FakeLifecycleRunner",
    "FakeRunnerCreation",
    "FakeSignalRelay",
    "FakeStoppableProcess",
    "GpuTelemetryContract",
    "HostExecutedContainer",
    "HostExecutedContainerBackend",
    "ProcessHarness",
    "RelayUnderTest",
    "ScriptedDockerCli",
    "SignalRelayContract",
    "StoppableProcessContract",
    "StoppableScript",
    "TelemetryHarness",
    "docker_missing",
    "docker_result",
    "docker_timed_out",
]
