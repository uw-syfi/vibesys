import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vs_sandbox.api import (
    ContentionStatus,
    CudaBackend,
    DockerSandbox,
    GpuInfo,
    HostResource,
    HostResourceAccess,
    SandboxKind,
)


class _FakeContentionEventSink:
    """Records every contention transition published to it."""

    def __init__(self) -> None:
        self.calls: list[ContentionStatus] = []

    def contention_changed(self, status: ContentionStatus) -> None:
        self.calls.append(status)


def _docker_run_command(sandbox: DockerSandbox) -> list[str]:
    result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="test-container\n", stderr=""
    )
    with patch("subprocess.run", return_value=result) as run:
        sandbox.start()
        command = next(
            call.args[0] for call in run.call_args_list if call.args[0][:2] == ["docker", "run"]
        )
        sandbox.stop()
    return command


def test_cpu_only_control_plane_docker_skips_gpu_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CudaBackend(tmp_path)
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
    assert "--gpus" not in _docker_run_command(sandbox)


def test_ephemeral_setup_sandbox_is_not_restarted_on_reselection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = CudaBackend(tmp_path)

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
        lambda: GpuInfo(1, "GPU-bbbb", "H100", 0, 100, 0),
    )
    monkeypatch.setattr("vs_sandbox.cuda_backend.GpuContentionMonitor", lambda **_kwargs: monitor)
    backend.reselect_device()
    start.assert_not_called()
    stop.assert_not_called()
    monitor.start.assert_called_once()


def test_make_monitor_forwards_the_injected_event_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = _FakeContentionEventSink()
    backend = CudaBackend(tmp_path, events=sink)
    backend.selected_device = GpuInfo(0, "GPU-aaaa", "H100", 0, 100, 0)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "vs_sandbox.cuda_backend.GpuContentionMonitor",
        lambda **kwargs: captured.update(kwargs) or MagicMock(),
    )

    backend.make_monitor(tmp_path)

    assert captured["events"] is sink


def test_reselect_device_forwards_the_injected_event_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = _FakeContentionEventSink()
    backend = CudaBackend(tmp_path, events=sink)
    monkeypatch.setattr(
        "vs_sandbox.cuda_backend.pick_gpu",
        lambda: GpuInfo(1, "GPU-bbbb", "H100", 0, 100, 0),
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "vs_sandbox.cuda_backend.GpuContentionMonitor",
        lambda **kwargs: captured.update(kwargs) or MagicMock(),
    )

    backend.reselect_device()

    assert captured["events"] is sink


def test_default_events_sink_is_a_no_op(tmp_path: Path) -> None:
    """Constructing a backend with no `events=` argument must not raise."""
    backend = CudaBackend(tmp_path)
    backend.selected_device = GpuInfo(0, "GPU-aaaa", "H100", 0, 100, 0)

    monitor = backend.make_monitor(tmp_path)

    assert monitor is not None


def test_docker_forwards_resources_to_the_sandbox(tmp_path: Path) -> None:
    backend = CudaBackend(tmp_path)
    resource = HostResource(tmp_path / "history", HostResourceAccess.READ_ONLY, "history")

    sandbox = backend.make_sandbox(
        SandboxKind.DOCKER,
        host_workspace=str(tmp_path),
        log_path=None,
        attach_accelerator=False,
        resources=[resource],
    )

    assert isinstance(sandbox, DockerSandbox)
    command = _docker_run_command(sandbox)
    assert f"{resource.path}:{resource.agent_path or resource.path}:ro" in command
