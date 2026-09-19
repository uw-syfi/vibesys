"""DeviceLease unit tests: env pinning, view gating, and gpu.json finalization."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from vibesys.constants import ComputeBackend
from vibesys.profilers import ProfilerKind
from vibesys.run import DeviceLease
from vibesys.sandbox.run_environment import AgentPaths, RunEnvironmentView

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.backends.base import ContentionMonitor, SandboxKind
    from vs_sandbox.execution import Sandbox


class _FakeDevice:
    """Device stand-in; the lease only reads ``index``."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.name = f"device-{index}"


class _FakeBackend:
    """``ComputeBackendImpl`` stand-in that selects no sandbox and no monitor."""

    name = ComputeBackend.CPU
    profiler_kind = ProfilerKind.NONE

    def __init__(self, selected_device: _FakeDevice | None = None) -> None:
        self.selected_device = selected_device

    def make_sandbox(self, kind: SandboxKind, **kwargs: Any) -> Sandbox:  # noqa: ANN401  # tracked: #288
        raise NotImplementedError

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:  # noqa: ARG002  # tracked: #288
        return None

    def reselect_device(self) -> None:
        return


def test_gpu_env_pins_selected_device(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = _FakeBackend(_FakeDevice(3))
    lease = DeviceLease(backend, log_dir=tmp_path)
    assert lease.gpu_env() == {"CUDA_VISIBLE_DEVICES": "3"}


def test_gpu_env_empty_without_device(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    lease = DeviceLease(_FakeBackend(), log_dir=tmp_path)
    assert lease.gpu_env() == {}


def test_reselect_skipped_when_view_disallows_host_reselect(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = MagicMock()
    view = RunEnvironmentView(paths=AgentPaths(), host_device_reselect=False)
    lease = DeviceLease(backend, log_dir=tmp_path, run_environment_view=view)
    lease.reselect()
    backend.reselect_device.assert_not_called()


def test_close_stops_monitor_and_finalizes_gpu_json(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    (tmp_path / "gpu.json").write_text(json.dumps({"name": "H100"}))
    (tmp_path / "gpu_contention.jsonl").write_text('{"is_contended": true}\n' * 2)

    backend = MagicMock()
    lease = DeviceLease(backend, log_dir=tmp_path)
    monitor = MagicMock()
    lease.monitor = monitor

    lease.close()

    monitor.stop.assert_called_once()
    data = json.loads((tmp_path / "gpu.json").read_text())
    assert data["contention_detected"] is True
    assert data["contention_events"] == 2
    assert "finished_at" in data


def test_close_without_gpu_json_is_a_noop(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    lease = DeviceLease(MagicMock(), log_dir=tmp_path)
    lease.close()
    assert not (tmp_path / "gpu.json").exists()
