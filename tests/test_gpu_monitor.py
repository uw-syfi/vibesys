"""Tests for GPU contention monitor."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from vibeserve_agent.backends.cuda.gpu_monitor import (
    GpuContentionMonitor,
    GpuInfo,
    _parse_proc_output,
    pick_gpu,
    query_gpu_info,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

GPU_A = "GPU-aaaa"
GPU_B = "GPU-bbbb"


def _gpu(index: int, uuid: str, used: int = 0, total: int = 81559, util: int = 0) -> GpuInfo:
    return GpuInfo(
        index=index, uuid=uuid, name="H100",
        memory_used_mib=used, memory_total_mib=total, utilization_pct=util,
    )


# ---------------------------------------------------------------------------
# GPU survey & selection
# ---------------------------------------------------------------------------


class TestQueryGpuInfo:
    @patch("vibeserve_agent.backends.cuda.gpu_monitor.subprocess.run")
    def test_parses_csv(self, mock_run):
        mock_run.return_value = type("R", (), {
            "returncode": 0,
            "stdout": f"0, {GPU_A}, H100, 5000, 81559, 30\n1, {GPU_B}, H100, 100, 81559, 0\n",
        })()
        gpus = query_gpu_info()
        assert len(gpus) == 2
        assert gpus[0].index == 0
        assert gpus[0].uuid == GPU_A
        assert gpus[0].memory_used_mib == 5000
        assert gpus[1].memory_free_mib == 81559 - 100

    @patch("vibeserve_agent.backends.cuda.gpu_monitor.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run):
        mock_run.return_value = type("R", (), {"returncode": 1, "stdout": ""})()
        assert query_gpu_info() == []

    @patch("vibeserve_agent.backends.cuda.gpu_monitor.subprocess.run")
    def test_returns_empty_on_missing_nvidia_smi(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        assert query_gpu_info() == []


class TestPickGpu:
    def test_picks_most_free_memory(self):
        gpus = [
            _gpu(0, GPU_A, used=70000),
            _gpu(1, GPU_B, used=100),
        ]
        best = pick_gpu(gpus)
        assert best is not None
        assert best.index == 1

    def test_empty_list(self):
        assert pick_gpu([]) is None

    def test_single_gpu(self):
        g = _gpu(0, GPU_A, used=5000)
        assert pick_gpu([g]) is g


# ---------------------------------------------------------------------------
# Process parsing
# ---------------------------------------------------------------------------


class TestParseProcOutput:
    def test_parses_four_columns(self):
        raw = f"1000, python, 4096, {GPU_A}\n"
        procs = _parse_proc_output(raw)
        assert len(procs) == 1
        assert procs[0]["gpu_uuid"] == GPU_A
        assert procs[0]["pid"] == 1000

    def test_empty(self):
        assert _parse_proc_output("") == []

    def test_skips_header(self):
        assert _parse_proc_output("pid, process_name, used_memory, gpu_uuid\n") == []

    def test_skips_short_rows(self):
        assert _parse_proc_output("100, python, 4096\n") == []


# ---------------------------------------------------------------------------
# Baseline-based contention detection
# ---------------------------------------------------------------------------


class TestMonitorLifecycle:
    @patch("vibeserve_agent.backends.cuda.gpu_monitor.query_gpu_info", return_value=[])
    @patch("vibeserve_agent.backends.cuda.gpu_monitor._query_gpu_procs")
    def test_start_stop(self, mock_procs, _mock_gpus):
        mock_procs.return_value = ""
        mon = GpuContentionMonitor(
            log_dir=Path("/tmp"), gpu_uuid=GPU_A, interval=0.05,
        )
        mon.start()
        assert mon._thread is not None
        assert mon._thread.is_alive()
        time.sleep(0.15)
        mon.stop()
        assert not mon._thread.is_alive()

    @patch("vibeserve_agent.backends.cuda.gpu_monitor.query_gpu_info", return_value=[])
    @patch("vibeserve_agent.backends.cuda.gpu_monitor._query_gpu_procs")
    def test_baseline_captured_on_start(self, mock_procs, _mock_gpus):
        """PIDs present at start() time become the baseline."""
        mock_procs.return_value = f"100, python, 4096, {GPU_A}\n"
        mon = GpuContentionMonitor(
            log_dir=Path("/tmp"), gpu_uuid=GPU_A, interval=0.05,
        )
        mon.start()
        assert 100 in mon._baseline_pids
        time.sleep(0.1)
        # Same PID still there → no contention
        assert not mon.status.is_contended
        mon.stop()

    @patch("vibeserve_agent.backends.cuda.gpu_monitor.query_gpu_info", return_value=[])
    @patch("vibeserve_agent.backends.cuda.gpu_monitor._query_gpu_procs")
    def test_new_pid_triggers_contention(self, mock_procs, _mock_gpus):
        """A PID that wasn't in the baseline triggers contention."""
        # Baseline: only PID 100
        mock_procs.return_value = f"100, python, 4096, {GPU_A}\n"
        mon = GpuContentionMonitor(
            log_dir=Path("/tmp"), gpu_uuid=GPU_A, interval=0.05,
        )
        mon.start()
        time.sleep(0.05)

        # New PID 200 appears on the same GPU
        mock_procs.return_value = (
            f"100, python, 4096, {GPU_A}\n"
            f"200, train.py, 8192, {GPU_A}\n"
        )
        time.sleep(0.15)
        status = mon.status
        mon.stop()
        assert status.is_contended
        assert any(p["pid"] == 200 for p in status.new_procs)

    @patch("vibeserve_agent.backends.cuda.gpu_monitor.query_gpu_info", return_value=[])
    @patch("vibeserve_agent.backends.cuda.gpu_monitor._query_gpu_procs")
    def test_new_pid_on_different_gpu_ignored(self, mock_procs, _mock_gpus):
        """A new PID on a different GPU is not contention."""
        mock_procs.return_value = f"100, python, 4096, {GPU_A}\n"
        mon = GpuContentionMonitor(
            log_dir=Path("/tmp"), gpu_uuid=GPU_A, interval=0.05,
        )
        mon.start()
        time.sleep(0.05)

        # PID 200 appears but on GPU_B
        mock_procs.return_value = (
            f"100, python, 4096, {GPU_A}\n"
            f"200, train.py, 8192, {GPU_B}\n"
        )
        time.sleep(0.15)
        status = mon.status
        mon.stop()
        assert not status.is_contended

    @patch("vibeserve_agent.backends.cuda.gpu_monitor.query_gpu_info", return_value=[])
    @patch("vibeserve_agent.backends.cuda.gpu_monitor._query_gpu_procs")
    def test_contention_logged_to_file(self, mock_procs, _mock_gpus, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        mock_procs.return_value = f"100, python, 4096, {GPU_A}\n"
        mon = GpuContentionMonitor(
            log_dir=log_dir, gpu_uuid=GPU_A, interval=0.05,
        )
        mon.start()
        time.sleep(0.05)

        mock_procs.return_value = (
            f"100, python, 4096, {GPU_A}\n"
            f"200, train.py, 8192, {GPU_A}\n"
        )
        time.sleep(0.2)
        mon.stop()

        log_file = log_dir / "gpu_contention.jsonl"
        assert log_file.exists()
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) >= 1
        event = json.loads(lines[0])
        assert event["is_contended"] is True
        assert event["gpu_uuid"] == GPU_A
        assert any(p["pid"] == 200 for p in event["new_procs"])

    @patch("vibeserve_agent.backends.cuda.gpu_monitor.query_gpu_info", return_value=[])
    @patch("vibeserve_agent.backends.cuda.gpu_monitor._query_gpu_procs")
    def test_no_log_when_no_contention(self, mock_procs, _mock_gpus, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        mock_procs.return_value = f"100, python, 4096, {GPU_A}\n"
        mon = GpuContentionMonitor(
            log_dir=log_dir, gpu_uuid=GPU_A, interval=0.05,
        )
        mon.start()
        time.sleep(0.15)
        mon.stop()

        log_file = log_dir / "gpu_contention.jsonl"
        if log_file.exists():
            assert log_file.read_text().strip() == ""

    @patch("vibeserve_agent.backends.cuda.gpu_monitor._query_gpu_procs")
    def test_smi_failure_does_not_crash(self, mock_procs, tmp_path):
        mock_procs.side_effect = Exception("nvidia-smi not found")
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        mon = GpuContentionMonitor(
            log_dir=log_dir, gpu_uuid=GPU_A, interval=0.05,
        )
        mon.start()
        time.sleep(0.15)
        mon.stop()
        assert not mon._thread.is_alive()

    def test_stop_without_start(self):
        mon = GpuContentionMonitor(log_dir=Path("/tmp"), gpu_uuid=GPU_A)
        mon.stop()  # should not raise


# ---------------------------------------------------------------------------
# reselect_gpu on _RunContext
# ---------------------------------------------------------------------------


class TestReselectGpu:
    """Tests for _RunContext.reselect_gpu()."""

    def _make_ctx(self, tmp_path, selected_gpu=None, use_docker=False):
        """Build a minimal _RunContext-like object for reselect_gpu testing."""
        from vibeserve_agent.backends.cuda import CudaBackend
        from vibeserve_agent.context import _RunContext
        from vibeserve_agent.sandbox.docker_sandbox import DockerSandbox

        ctx = object.__new__(_RunContext)
        ctx.selected_gpu = selected_gpu
        ctx.log_dir = tmp_path / "logs"
        ctx.log_dir.mkdir(parents=True, exist_ok=True)
        ctx.gpu_monitor = None
        ctx._docker_symlinks = []

        # Real CudaBackend so reselect_gpu's delegation hits the actual logic;
        # tests patch pick_gpu / query_gpu_info to control device selection.
        backend_impl = CudaBackend(log_dir=ctx.log_dir, log=lambda _msg: None)
        backend_impl.selected_device = selected_gpu
        ctx.backend_impl = backend_impl

        # Sandboxes — DockerSandbox spec'd MagicMock so isinstance checks
        # in reselect_device route through the docker branch.  Register with
        # the backend so reselect_device's iteration over self._sandboxes
        # finds them.
        from vibeserve_agent.backends.base import SandboxKind
        if use_docker:
            ctx.implementer_backend = MagicMock(spec=DockerSandbox)
            ctx.judge_backend = MagicMock(spec=DockerSandbox)
            kind = SandboxKind.DOCKER
        else:
            from deepagents.backends import LocalShellBackend
            ctx.implementer_backend = MagicMock(spec=LocalShellBackend)
            ctx.judge_backend = MagicMock(spec=LocalShellBackend)
            # _env mutated by reselect_device — give it a real dict.
            ctx.implementer_backend._env = {}
            ctx.judge_backend._env = {}
            kind = SandboxKind.LOCAL
        backend_impl._sandboxes = [
            (kind, ctx.implementer_backend),
            (kind, ctx.judge_backend),
        ]

        ctx.run_log_file = MagicMock()
        return ctx

    @patch("vibeserve_agent.backends.cuda.pick_gpu")
    def test_noop_when_cuda_visible_set(self, mock_pick, tmp_path):
        ctx = self._make_ctx(tmp_path, selected_gpu=_gpu(0, GPU_A))
        with patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": "0"}):
            ctx.reselect_gpu()
        mock_pick.assert_not_called()

    @patch("vibeserve_agent.backends.cuda.pick_gpu", return_value=None)
    def test_noop_when_no_gpus(self, mock_pick, tmp_path):
        ctx = self._make_ctx(tmp_path, selected_gpu=_gpu(0, GPU_A))
        ctx.reselect_gpu()
        assert ctx.selected_gpu.index == 0  # unchanged

    @patch("vibeserve_agent.backends.cuda.pick_gpu")
    def test_noop_when_same_gpu(self, mock_pick, tmp_path):
        gpu0 = _gpu(0, GPU_A, used=100)
        ctx = self._make_ctx(tmp_path, selected_gpu=gpu0)
        mock_pick.return_value = _gpu(0, GPU_A, used=200)
        ctx.reselect_gpu()
        # Still the original object (not updated since index matches)
        assert ctx.selected_gpu is gpu0

    @patch("vibeserve_agent.backends.cuda.gpu_monitor.query_gpu_info", return_value=[])
    @patch("vibeserve_agent.backends.cuda.pick_gpu")
    @patch("vibeserve_agent.backends.cuda.gpu_monitor._query_gpu_procs", return_value="")
    def test_local_backend_env_updated(self, _mock_procs, mock_pick, _mock_query, tmp_path):
        """When GPU changes, local backends get updated CUDA_VISIBLE_DEVICES."""
        gpu0 = _gpu(0, GPU_A, used=5000)
        gpu1 = _gpu(1, GPU_B, used=100)
        ctx = self._make_ctx(tmp_path, selected_gpu=gpu0, use_docker=False)
        ctx.implementer_backend._env["CUDA_VISIBLE_DEVICES"] = "0"
        ctx.judge_backend._env["CUDA_VISIBLE_DEVICES"] = "0"

        mock_pick.return_value = gpu1
        ctx.reselect_gpu()

        assert ctx.selected_gpu is gpu1
        assert ctx.implementer_backend._env["CUDA_VISIBLE_DEVICES"] == "1"
        assert ctx.judge_backend._env["CUDA_VISIBLE_DEVICES"] == "1"

    @patch("vibeserve_agent.backends.cuda.gpu_monitor.query_gpu_info", return_value=[])
    @patch("vibeserve_agent.backends.cuda.pick_gpu")
    @patch("vibeserve_agent.backends.cuda.gpu_monitor._query_gpu_procs", return_value="")
    def test_contention_monitor_restarted(self, _mock_procs, mock_pick, _mock_query, tmp_path):
        """Contention monitor switches to the new GPU UUID."""
        gpu0 = _gpu(0, GPU_A, used=5000)
        gpu1 = _gpu(1, GPU_B, used=100)
        ctx = self._make_ctx(tmp_path, selected_gpu=gpu0, use_docker=False)

        # Initial monitor lives on the backend (matches the production flow
        # where _RunContext.__init__ binds ctx.gpu_monitor to the same object).
        old_monitor = MagicMock()
        ctx.backend_impl._monitor = old_monitor
        ctx.gpu_monitor = old_monitor

        mock_pick.return_value = gpu1
        ctx.reselect_gpu()

        old_monitor.stop.assert_called_once()
        assert ctx.gpu_monitor is not old_monitor
        assert ctx.gpu_monitor._gpu_uuid == GPU_B
        # Clean up
        ctx.gpu_monitor.stop()

    @patch("vibeserve_agent.backends.cuda.gpu_monitor.query_gpu_info", return_value=[])
    @patch("vibeserve_agent.backends.cuda.pick_gpu")
    @patch("vibeserve_agent.backends.cuda.gpu_monitor._query_gpu_procs", return_value="")
    def test_docker_backends_restarted(self, _mock_procs, mock_pick, _mock_query, tmp_path):
        """Docker backends are stopped, updated, and restarted on GPU change."""
        gpu0 = _gpu(0, GPU_A, used=5000)
        gpu1 = _gpu(1, GPU_B, used=100)
        ctx = self._make_ctx(tmp_path, selected_gpu=gpu0, use_docker=True)

        mock_pick.return_value = gpu1
        ctx.reselect_gpu()

        ctx.implementer_backend.stop.assert_called_once()
        ctx.judge_backend.stop.assert_called_once()
        assert ctx.implementer_backend._gpus == "device=1"
        assert ctx.judge_backend._gpus == "device=1"
        ctx.implementer_backend.start.assert_called_once()
        ctx.judge_backend.start.assert_called_once()
        # Clean up
        ctx.gpu_monitor.stop()

    # Note: symlink replay on restart is now the sandbox class's
    # responsibility (it runs setup_fns at the end of start()).  That
    # behaviour is tested in tests/test_docker_sandbox.py; reselect_gpu
    # itself just calls sb.stop()/sb.start() and the rest happens for free.

    @patch("vibeserve_agent.backends.cuda.gpu_monitor.query_gpu_info", return_value=[])
    @patch("vibeserve_agent.backends.cuda.pick_gpu")
    @patch("vibeserve_agent.backends.cuda.gpu_monitor._query_gpu_procs", return_value="")
    def test_first_selection_from_none(self, _mock_procs, mock_pick, _mock_query, tmp_path):
        """Works when selected_gpu was initially None (no GPU at startup)."""
        gpu1 = _gpu(1, GPU_B, used=100)
        ctx = self._make_ctx(tmp_path, selected_gpu=None, use_docker=False)

        mock_pick.return_value = gpu1
        ctx.reselect_gpu()

        assert ctx.selected_gpu is gpu1
        assert ctx.implementer_backend._env["CUDA_VISIBLE_DEVICES"] == "1"
        # Clean up
        ctx.gpu_monitor.stop()
