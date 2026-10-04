"""DeviceLease unit tests: env pinning, view gating, and gpu.json finalization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from tests.support.file_effects import file_size_limit

from vs_sandbox.api import ComputeBackend, DeviceLease

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sandbox.api import Sandbox, SandboxKind


@dataclass
class _RecordingMonitor:
    started: int = 0
    stopped: int = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


@dataclass(frozen=True)
class _View:
    host_device_reselect: bool


class _FakeDevice:
    """Device stand-in; the lease only reads ``index``."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.name = f"device-{index}"


class _FakeBackend:
    """``ComputeBackendImpl`` stand-in that selects no sandbox and no monitor."""

    name = ComputeBackend.CPU

    def __init__(
        self,
        selected_device: _FakeDevice | None = None,
        monitor: _RecordingMonitor | None = None,
    ) -> None:
        self.selected_device = selected_device
        self._monitor = monitor
        self.reselection_count = 0

    def make_sandbox(self, kind: SandboxKind, **kwargs: object) -> Sandbox:
        raise NotImplementedError

    def make_monitor(self, log_dir: Path) -> _RecordingMonitor | None:
        del log_dir
        return self._monitor

    def reselect_device(self) -> None:
        self.reselection_count += 1


def test_gpu_env_pins_selected_device(tmp_path: Path) -> None:
    backend = _FakeBackend(_FakeDevice(3))
    lease = DeviceLease(backend, log_dir=tmp_path)
    assert lease.gpu_env() == {"CUDA_VISIBLE_DEVICES": "3"}


def test_gpu_env_empty_without_device(tmp_path: Path) -> None:
    lease = DeviceLease(_FakeBackend(), log_dir=tmp_path)
    assert lease.gpu_env() == {}


def test_reselect_skipped_when_view_disallows_host_reselect(tmp_path: Path) -> None:
    backend = _FakeBackend()
    lease = DeviceLease(
        backend,
        log_dir=tmp_path,
        run_environment_view=_View(host_device_reselect=False),
    )
    lease.reselect()
    assert backend.reselection_count == 0


def test_reselect_delegates_and_adopts_backend_monitor(tmp_path: Path) -> None:
    monitor = _RecordingMonitor()
    backend = _FakeBackend(monitor=monitor)
    lease = DeviceLease(backend, log_dir=tmp_path)

    lease.reselect()

    assert backend.reselection_count == 1
    assert lease.monitor is monitor


def test_close_stops_monitor_and_finalizes_gpu_json(tmp_path: Path) -> None:
    (tmp_path / "gpu.json").write_text(json.dumps({"name": "H100"}))
    (tmp_path / "gpu_contention.jsonl").write_text('{"is_contended": true}\n' * 2)

    monitor = _RecordingMonitor()
    backend = _FakeBackend(monitor=monitor)
    lease = DeviceLease(backend, log_dir=tmp_path)
    lease.monitor = monitor

    lease.close()

    assert monitor.stopped == 1
    data = json.loads((tmp_path / "gpu.json").read_text())
    assert data["contention_detected"] is True
    assert data["contention_events"] == 2
    assert "finished_at" in data


def test_close_without_gpu_json_is_a_noop(tmp_path: Path) -> None:
    lease = DeviceLease(_FakeBackend(), log_dir=tmp_path)
    lease.close()
    assert not (tmp_path / "gpu.json").exists()


def test_close_interrupted_at_every_byte_preserves_gpu_metadata(tmp_path: Path) -> None:
    path = tmp_path / "gpu.json"
    old = b'{"name":"device"}'
    lease = DeviceLease(_FakeBackend(), log_dir=tmp_path)
    for interruption in range(100):
        path.write_bytes(old)
        with file_size_limit(interruption), pytest.raises(OSError, match="File too large"):
            lease.close()
        assert path.read_bytes() == old
        assert not list(path.parent.glob(".*.tmp"))
