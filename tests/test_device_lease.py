"""DeviceLease unit tests: env pinning, view gating, and gpu.json finalization."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from vibesys.run import DeviceLease


def test_gpu_env_pins_selected_device(tmp_path):
    backend = SimpleNamespace(selected_device=SimpleNamespace(index=3))
    lease = DeviceLease(backend, log_dir=tmp_path)
    assert lease.gpu_env() == {"CUDA_VISIBLE_DEVICES": "3"}


def test_gpu_env_empty_without_device(tmp_path):
    lease = DeviceLease(SimpleNamespace(), log_dir=tmp_path)
    assert lease.gpu_env() == {}


def test_reselect_skipped_when_view_disallows_host_reselect(tmp_path):
    backend = MagicMock()
    view = SimpleNamespace(host_device_reselect=False)
    lease = DeviceLease(backend, log_dir=tmp_path, run_environment_view=view)
    lease.reselect()
    backend.reselect_device.assert_not_called()


def test_close_stops_monitor_and_finalizes_gpu_json(tmp_path):
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


def test_close_without_gpu_json_is_a_noop(tmp_path):
    lease = DeviceLease(MagicMock(), log_dir=tmp_path)
    lease.close()
    assert not (tmp_path / "gpu.json").exists()
