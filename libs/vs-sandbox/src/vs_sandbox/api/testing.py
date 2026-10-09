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
from vs_sandbox.fake_docker_command import DockerCommandCall, FakeDockerCommandRunner
from vs_sandbox.fake_docker_engine import FakeDockerEngine

__all__ = [
    "DEFAULT_RESULT",
    "DockerCommandCall",
    "FakeAcceleratorDiscovery",
    "FakeCommandRunner",
    "FakeComputeBackend",
    "FakeDockerCommandRunner",
    "FakeDockerEngine",
    "FakeExecution",
    "FakeLifecycleRunner",
    "FakeRunnerCreation",
]
