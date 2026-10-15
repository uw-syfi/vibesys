from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tests.support.docker import capture_docker_start_argv, recording_docker_cli

from vs_sandbox.api import (
    CudaAcceleratorUnavailableError,
    CudaBackend,
    DockerSandbox,
    GpuInfo,
    HostResource,
    HostResourceAccess,
    SandboxKind,
)
from vs_sandbox.api.testing import FakeGpuTelemetry, ScriptedDockerCli
from vs_sim.api.testing import FakePipedProcessLauncher, ManualClock, ScriptedProbe, SimThreads


def test_cpu_only_control_plane_docker_skips_gpu_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = recording_docker_cli()
    backend = CudaBackend(
        tmp_path,
        docker=docker,
        clock=ManualClock(),
        threads=SimThreads(),
        launcher=FakePipedProcessLauncher(),
        probe=ScriptedProbe(lambda _argv: None),
    )
    pick_device = MagicMock()
    monkeypatch.setattr("vs_sandbox.cuda_backend.pick_gpu", pick_device)

    sandbox = backend.make_sandbox(
        SandboxKind.DOCKER,
        host_workspace=str(tmp_path),
        log_path=None,
        attach_accelerator=False,
    )

    pick_device.assert_not_called()
    assert isinstance(sandbox, DockerSandbox)
    assert backend.selected_device is None
    assert "--gpus" not in capture_docker_start_argv(sandbox, docker)


def test_ephemeral_setup_sandbox_is_not_restarted_on_reselection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = CudaBackend(
        tmp_path,
        clock=ManualClock(),
        threads=SimThreads(),
        docker=ScriptedDockerCli(),
        launcher=FakePipedProcessLauncher(),
        probe=ScriptedProbe(lambda _argv: None),
    )

    sandbox = backend.make_sandbox(
        SandboxKind.DOCKER,
        host_workspace=str(tmp_path),
        log_path=None,
        attach_accelerator=False,
        ephemeral=True,
    )

    assert isinstance(sandbox, DockerSandbox)
    start = MagicMock()
    stop = MagicMock()
    monkeypatch.setattr(sandbox, "start", start)
    monkeypatch.setattr(sandbox, "stop", stop)
    monitor = MagicMock()
    monkeypatch.setattr(
        "vs_sandbox.cuda_backend.pick_gpu",
        lambda _gpus: GpuInfo(1, "GPU-bbbb", "H100", 0, 100, 0),
    )
    monkeypatch.setattr("vs_sandbox.cuda_backend.GpuContentionMonitor", lambda **_kwargs: monitor)
    backend.reselect_device()
    start.assert_not_called()
    stop.assert_not_called()
    monitor.start.assert_called_once()


def test_docker_forwards_resources_to_the_sandbox(tmp_path: Path) -> None:
    docker = recording_docker_cli()
    backend = CudaBackend(
        tmp_path,
        docker=docker,
        clock=ManualClock(),
        threads=SimThreads(),
        launcher=FakePipedProcessLauncher(),
        probe=ScriptedProbe(lambda _argv: None),
    )
    resource = HostResource(tmp_path / "history", HostResourceAccess.READ_ONLY, "history")

    sandbox = backend.make_sandbox(
        SandboxKind.DOCKER,
        host_workspace=str(tmp_path),
        log_path=None,
        attach_accelerator=False,
        resources=[resource],
    )

    assert isinstance(sandbox, DockerSandbox)
    command = capture_docker_start_argv(sandbox, docker)
    assert f"{resource.path}:{resource.agent_path or resource.path}:ro" in command


def test_ensure_accelerator_available_rejects_a_host_with_no_gpu(tmp_path: Path) -> None:
    backend = CudaBackend(
        tmp_path,
        gpu_telemetry=FakeGpuTelemetry(gpus=[]),
        clock=ManualClock(),
        threads=SimThreads(),
        docker=ScriptedDockerCli(),
        launcher=FakePipedProcessLauncher(),
        probe=ScriptedProbe(lambda _argv: None),
    )

    with pytest.raises(CudaAcceleratorUnavailableError, match="NVIDIA GPU"):
        backend.ensure_accelerator_available()


def test_ensure_accelerator_available_accepts_a_host_with_a_gpu(tmp_path: Path) -> None:
    gpu = GpuInfo(0, "GPU-aaaa", "H100", 0, 100, 0)
    backend = CudaBackend(
        tmp_path,
        gpu_telemetry=FakeGpuTelemetry(gpus=[gpu]),
        clock=ManualClock(),
        threads=SimThreads(),
        docker=ScriptedDockerCli(),
        launcher=FakePipedProcessLauncher(),
        probe=ScriptedProbe(lambda _argv: None),
    )

    backend.ensure_accelerator_available()  # must not raise


def test_ensure_accelerator_available_trusts_a_pinned_cuda_visible_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user who pinned a GPU index is trusted, matching device-selection's own exception."""
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    backend = CudaBackend(
        tmp_path,
        gpu_telemetry=FakeGpuTelemetry(gpus=[]),
        clock=ManualClock(),
        threads=SimThreads(),
        docker=ScriptedDockerCli(),
        launcher=FakePipedProcessLauncher(),
        probe=ScriptedProbe(lambda _argv: None),
    )

    backend.ensure_accelerator_available()  # must not raise
