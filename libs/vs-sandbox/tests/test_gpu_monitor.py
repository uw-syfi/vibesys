"""Tests for GPU contention monitor."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sandbox.api import (
    CudaBackend,
    DeviceLease,
    DockerSandbox,
    GpuContentionMonitor,
    GpuInfo,
    LocalShellRunner,
    SandboxKind,
    parse_gpu_info_output,
    parse_gpu_process_output,
    pick_gpu,
)
from vs_sandbox.api.testing import (
    FakeGpuTelemetry,
    GpuTelemetryContract,
    ScriptedDockerCli,
    TelemetryHarness,
    docker_result,
)
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

GPU_A = "GPU-aaaa"
GPU_B = "GPU-bbbb"


def _gpu(index: int, uuid: str, used: int = 0, total: int = 81559, util: int = 0) -> GpuInfo:
    return GpuInfo(
        index=index,
        uuid=uuid,
        name="H100",
        memory_used_mib=used,
        memory_total_mib=total,
        utilization_pct=util,
    )


# ---------------------------------------------------------------------------
# GPU survey & selection
# ---------------------------------------------------------------------------


class TestParseGpuInfo:
    def test_parses_csv(self) -> None:
        gpus = parse_gpu_info_output(
            f"0, {GPU_A}, H100, 5000, 81559, 30\n1, {GPU_B}, H100, 100, 81559, 0\n"
        )
        assert len(gpus) == 2
        assert gpus[0].index == 0
        assert gpus[0].uuid == GPU_A
        assert gpus[0].memory_used_mib == 5000
        assert gpus[1].memory_free_mib == 81559 - 100

    def test_empty_output_has_no_gpus(self) -> None:
        assert parse_gpu_info_output("") == []

    def test_skips_malformed_rows(self) -> None:
        raw = f"0, {GPU_A}, H100, many, 81559, 30\nshort, row\n1, {GPU_B}, H100, 100, 81559, 0\n"
        assert [g.uuid for g in parse_gpu_info_output(raw)] == [GPU_B]

    @given(
        st.lists(
            st.tuples(st.integers(0, 15), st.integers(0, 2**17), st.integers(0, 100)),
            max_size=8,
        )
    )
    def test_round_trips_any_well_formed_rows(self, rows: list[tuple[int, int, int]]) -> None:
        raw = "".join(f"{i}, GPU-{i}, H100, {used}, 131072, {util}\n" for i, used, util in rows)
        assert [
            (g.index, g.memory_used_mib, g.utilization_pct) for g in parse_gpu_info_output(raw)
        ] == rows


class TestFakeGpuTelemetry(GpuTelemetryContract):
    def harness(self) -> TelemetryHarness:
        return TelemetryHarness(
            reporting=FakeGpuTelemetry,
            without_driver=FakeGpuTelemetry,
            failing=FakeGpuTelemetry,
        )


class TestPickGpu:
    def test_picks_most_free_memory(self) -> None:
        gpus = [
            _gpu(0, GPU_A, used=70000),
            _gpu(1, GPU_B, used=100),
        ]
        best = pick_gpu(gpus)
        assert best is not None
        assert best.index == 1

    def test_empty_list(self) -> None:
        assert pick_gpu([]) is None

    def test_single_gpu(self) -> None:
        g = _gpu(0, GPU_A, used=5000)
        assert pick_gpu([g]) is g


# ---------------------------------------------------------------------------
# Process parsing
# ---------------------------------------------------------------------------


class TestParseProcOutput:
    def test_parses_four_columns(self) -> None:
        raw = f"1000, python, 4096, {GPU_A}\n"
        procs = parse_gpu_process_output(raw)
        assert len(procs) == 1
        assert procs[0]["gpu_uuid"] == GPU_A
        assert procs[0]["pid"] == 1000

    def test_empty(self) -> None:
        assert parse_gpu_process_output("") == []

    def test_skips_header(self) -> None:
        assert parse_gpu_process_output("pid, process_name, used_memory, gpu_uuid\n") == []

    def test_skips_short_rows(self) -> None:
        assert parse_gpu_process_output("100, python, 4096\n") == []


# ---------------------------------------------------------------------------
# Baseline-based contention detection
# ---------------------------------------------------------------------------


NEW_PROC = "200, train.py, 8192"
BASELINE_PROC = "100, python, 4096"
_INTERVAL = 30.0


def _rows(*rows: tuple[str, str]) -> str:
    return "".join(f"{proc}, {uuid}\n" for proc, uuid in rows)


class TestMonitorLifecycle:
    def _monitor(
        self, tmp_path: Path, telemetry: FakeGpuTelemetry, threads: SimThreads
    ) -> GpuContentionMonitor:
        return GpuContentionMonitor(
            log_dir=tmp_path,
            gpu_uuid=GPU_A,
            interval=_INTERVAL,
            telemetry=telemetry,
            threads=threads,
            clock=ManualClock(1000.0),
        )

    def test_start_stop(self, tmp_path: Path) -> None:
        telemetry = FakeGpuTelemetry()
        threads = SimThreads()
        mon = self._monitor(tmp_path, telemetry, threads)

        def case() -> None:
            mon.start()
            threads.sleep(_INTERVAL * 3)
            mon.stop()
            reads = telemetry.process_reads
            threads.sleep(_INTERVAL * 3)
            assert reads > 1
            assert telemetry.process_reads == reads

        threads.run(case)

    def test_baseline_captured_on_start(self, tmp_path: Path) -> None:
        """PIDs present at start() time become the baseline."""
        telemetry = FakeGpuTelemetry(processes=_rows((BASELINE_PROC, GPU_A)))
        threads = SimThreads()
        mon = self._monitor(tmp_path, telemetry, threads)

        def case() -> None:
            mon.start()
            threads.sleep(_INTERVAL * 2)
            assert not mon.status.is_contended
            mon.stop()

        threads.run(case)

    def test_new_pid_triggers_contention(self, tmp_path: Path) -> None:
        """A PID that wasn't in the baseline triggers contention."""
        telemetry = FakeGpuTelemetry(processes=_rows((BASELINE_PROC, GPU_A)))
        threads = SimThreads()
        mon = self._monitor(tmp_path, telemetry, threads)

        def case() -> None:
            mon.start()
            threads.sleep(_INTERVAL)
            telemetry.processes = _rows((BASELINE_PROC, GPU_A), (NEW_PROC, GPU_A))
            threads.sleep(_INTERVAL * 2)
            status = mon.status
            mon.stop()
            assert status.is_contended
            assert any(p["pid"] == 200 for p in status.new_procs)

        threads.run(case)

    def test_new_pid_on_different_gpu_ignored(self, tmp_path: Path) -> None:
        """A new PID on a different GPU is not contention."""
        telemetry = FakeGpuTelemetry(processes=_rows((BASELINE_PROC, GPU_A)))
        threads = SimThreads()
        mon = self._monitor(tmp_path, telemetry, threads)

        def case() -> None:
            mon.start()
            threads.sleep(_INTERVAL)
            telemetry.processes = _rows((BASELINE_PROC, GPU_A), (NEW_PROC, GPU_B))
            threads.sleep(_INTERVAL * 2)
            status = mon.status
            mon.stop()
            assert not status.is_contended

        threads.run(case)

    def test_contention_logged_to_file(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        telemetry = FakeGpuTelemetry(
            gpus=[_gpu(0, GPU_A, used=1)], processes=_rows((BASELINE_PROC, GPU_A))
        )
        threads = SimThreads()
        mon = self._monitor(log_dir, telemetry, threads)

        def case() -> None:
            mon.start()
            threads.sleep(_INTERVAL)
            telemetry.processes = _rows((BASELINE_PROC, GPU_A), (NEW_PROC, GPU_A))
            threads.sleep(_INTERVAL * 2)
            mon.stop()

        threads.run(case)

        lines = (log_dir / "gpu_contention.jsonl").read_text().strip().split("\n")
        event = json.loads(lines[0])
        assert event["is_contended"] is True
        assert event["gpu_uuid"] == GPU_A
        assert event["timestamp"] == datetime.fromtimestamp(1000.0, UTC).isoformat()
        assert event["gpu"]["memory_used_mib"] == 1
        assert any(p["pid"] == 200 for p in event["new_procs"])

    def test_no_log_when_no_contention(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        telemetry = FakeGpuTelemetry(processes=_rows((BASELINE_PROC, GPU_A)))
        threads = SimThreads()
        mon = self._monitor(log_dir, telemetry, threads)

        def case() -> None:
            mon.start()
            threads.sleep(_INTERVAL * 3)
            mon.stop()

        threads.run(case)
        assert not (log_dir / "gpu_contention.jsonl").exists()

    def test_smi_failure_does_not_crash(self, tmp_path: Path) -> None:
        telemetry = FakeGpuTelemetry()
        telemetry.fail_with = RuntimeError("nvidia-smi not found")
        threads = SimThreads()
        mon = self._monitor(tmp_path, telemetry, threads)

        def case() -> None:
            mon.start()
            threads.sleep(_INTERVAL * 3)
            telemetry.fail_with = None
            reads = telemetry.process_reads
            threads.sleep(_INTERVAL * 2)
            mon.stop()
            # The loop survived the failures and kept polling.
            assert telemetry.process_reads > reads
            assert not mon.status.is_contended

        threads.run(case)

    def test_stop_without_start(self, tmp_path: Path) -> None:
        mon = GpuContentionMonitor(log_dir=tmp_path, gpu_uuid=GPU_A, threads=SimThreads())
        mon.stop()  # should not raise

    @settings(deadline=None, max_examples=60)
    @given(
        baseline=st.sets(st.integers(1, 6)),
        later=st.dictionaries(st.integers(1, 12), st.sampled_from([GPU_A, GPU_B])),
        seed=st.one_of(st.none(), st.integers(0, 2**32)),
    )
    def test_contended_exactly_when_a_new_pid_is_on_the_monitored_gpu(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        baseline: set[int],
        later: dict[int, str],
        seed: int | None,
    ) -> None:
        telemetry = FakeGpuTelemetry(
            processes=_rows(*((f"{pid}, p, 1", GPU_A) for pid in sorted(baseline)))
        )
        threads = SimThreads(schedule_seed=seed)
        mon = self._monitor(tmp_path_factory.mktemp("gpu"), telemetry, threads)

        def case() -> None:
            mon.start()
            telemetry.processes = _rows(*((f"{pid}, p, 1", uuid) for pid, uuid in later.items()))
            threads.sleep(_INTERVAL * 2)
            status = mon.status
            mon.stop()
            expected = {pid for pid, uuid in later.items() if uuid == GPU_A and pid not in baseline}
            assert status.is_contended == bool(expected)
            assert {p["pid"] for p in status.new_procs} == expected

        threads.run(case)


# ---------------------------------------------------------------------------
# device lease reselection
# ---------------------------------------------------------------------------


class TestReselectGpu:
    """Tests for DeviceLease.reselect()."""

    def _make_ctx(
        self,
        tmp_path: Path,
        *,
        selected_gpu: GpuInfo | None = None,
        use_docker: bool = False,
        telemetry: FakeGpuTelemetry | None = None,
    ) -> SimpleNamespace:
        """Build a device lease with registered CUDA sandboxes."""

        ctx = SimpleNamespace(log_dir=tmp_path / "logs")
        ctx.log_dir.mkdir(parents=True, exist_ok=True)

        # Real CudaBackend so reselect_gpu's delegation hits the actual logic; the
        # fake telemetry and simulated threads control device selection and monitoring.
        ctx.threads = SimThreads()
        docker = ScriptedDockerCli()
        docker.always(docker_result(stdout="container-1\n"))
        ctx.telemetry = telemetry or FakeGpuTelemetry()
        backend_impl = CudaBackend(
            log_dir=ctx.log_dir,
            log=lambda _msg: None,
            gpu_telemetry=ctx.telemetry,
            threads=ctx.threads,
            docker=docker,
        )
        backend_impl.selected_device = selected_gpu
        ctx.backend_impl = backend_impl

        # DeviceLease coordinates reselection and monitor ownership.

        ctx.device = DeviceLease(backend_impl, log_dir=ctx.log_dir)

        # Sandboxes built through the backend are registered with it, so
        # reselect_device finds them; Docker ones talk to a scripted CLI.

        if use_docker:
            ctx.docker = docker
            ctx.implementer_backend = backend_impl.make_sandbox(
                SandboxKind.DOCKER,
                host_workspace=str(ctx.log_dir / "implementer"),
                log_path=None,
                attach_accelerator=False,
            )
            ctx.judge_backend = backend_impl.make_sandbox(
                SandboxKind.DOCKER,
                host_workspace=str(ctx.log_dir / "judge"),
                log_path=None,
                attach_accelerator=False,
            )
        else:
            implementer_backend = cast(
                "LocalShellRunner",
                backend_impl.make_sandbox(
                    SandboxKind.LOCAL,
                    host_workspace=str(ctx.log_dir / "implementer"),
                    log_path=None,
                    attach_accelerator=False,
                ),
            )
            judge_backend = cast(
                "LocalShellRunner",
                backend_impl.make_sandbox(
                    SandboxKind.LOCAL,
                    host_workspace=str(ctx.log_dir / "judge"),
                    log_path=None,
                    attach_accelerator=False,
                ),
            )
            # env mutated by reselect_device — give it a real dict.
            implementer_backend.env = {}
            judge_backend.env = {}
            ctx.implementer_backend = implementer_backend
            ctx.judge_backend = judge_backend

        return ctx

    def test_noop_when_cuda_visible_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gpu0 = _gpu(0, GPU_A)
        ctx = self._make_ctx(
            tmp_path,
            selected_gpu=gpu0,
            telemetry=FakeGpuTelemetry([_gpu(1, GPU_B, used=0), _gpu(0, GPU_A, used=9000)]),
        )
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
        ctx.device.reselect()
        assert ctx.device.selected_device is gpu0

    def test_noop_when_no_gpus(self, tmp_path: Path) -> None:
        ctx = self._make_ctx(tmp_path, selected_gpu=_gpu(0, GPU_A))
        ctx.device.reselect()
        assert ctx.device.selected_device is not None
        assert ctx.device.selected_device.index == 0  # unchanged

    def test_noop_when_same_gpu(self, tmp_path: Path) -> None:
        gpu0 = _gpu(0, GPU_A, used=100)
        ctx = self._make_ctx(
            tmp_path, selected_gpu=gpu0, telemetry=FakeGpuTelemetry([_gpu(0, GPU_A, used=200)])
        )
        ctx.device.reselect()
        # Still the original object (not updated since index matches)
        assert ctx.device.selected_device is gpu0

    def test_local_backend_env_updated(self, tmp_path: Path) -> None:
        """When GPU changes, local backends get updated CUDA_VISIBLE_DEVICES."""
        gpu0 = _gpu(0, GPU_A, used=5000)
        gpu1 = _gpu(1, GPU_B, used=100)
        ctx = self._make_ctx(
            tmp_path,
            selected_gpu=gpu0,
            use_docker=False,
            telemetry=FakeGpuTelemetry([gpu0, gpu1]),
        )
        implementer_backend = cast("LocalShellRunner", ctx.implementer_backend)
        judge_backend = cast("LocalShellRunner", ctx.judge_backend)
        implementer_backend.env["CUDA_VISIBLE_DEVICES"] = "0"
        judge_backend.env["CUDA_VISIBLE_DEVICES"] = "0"

        def case() -> None:
            ctx.device.reselect()
            ctx.device.monitor.stop()

        ctx.threads.run(case)

        assert ctx.device.selected_device is gpu1
        assert implementer_backend.env["CUDA_VISIBLE_DEVICES"] == "1"
        assert judge_backend.env["CUDA_VISIBLE_DEVICES"] == "1"

    def test_contention_monitor_restarted(self, tmp_path: Path) -> None:
        """Contention monitor switches to the new GPU UUID."""
        gpu0 = _gpu(0, GPU_A, used=5000)
        gpu1 = _gpu(1, GPU_B, used=100)
        telemetry = FakeGpuTelemetry([gpu0, gpu1])
        ctx = self._make_ctx(tmp_path, selected_gpu=gpu0, use_docker=False, telemetry=telemetry)

        def case() -> None:
            # The first monitor lives on the backend (matches the production flow
            # where DeviceLease owns the backend monitor).
            ctx.device.start_monitor()
            old_monitor = ctx.device.monitor
            ctx.device.reselect()
            new_monitor = ctx.device.monitor
            assert new_monitor is not old_monitor
            # A process appearing on the new GPU is contention only for the new monitor.
            telemetry.processes = f"900, other, 10, {GPU_B}\n"
            ctx.threads.sleep(60.0)
            assert new_monitor.status.is_contended
            assert not old_monitor.status.is_contended
            new_monitor.stop()

        ctx.threads.run(case)

    def test_docker_backends_restarted(self, tmp_path: Path) -> None:
        """Docker backends are stopped, updated, and restarted on GPU change."""
        gpu0 = _gpu(0, GPU_A, used=5000)
        gpu1 = _gpu(1, GPU_B, used=100)
        ctx = self._make_ctx(
            tmp_path, selected_gpu=gpu0, use_docker=True, telemetry=FakeGpuTelemetry([gpu0, gpu1])
        )

        def case() -> None:
            ctx.device.reselect()
            assert ctx.device.monitor is not None
            ctx.device.monitor.stop()

        ctx.threads.run(case)

        assert isinstance(ctx.implementer_backend, DockerSandbox)
        assert isinstance(ctx.judge_backend, DockerSandbox)
        runs = [argv for argv in ctx.docker.argvs if argv[:2] == ("docker", "run")]
        assert len(runs) == 2
        assert all("device=1" in argv for argv in runs)

    # Note: symlink replay on restart is the sandbox class's responsibility
    # (it runs lifecycle hooks before becoming ready).

    def test_first_selection_from_none(self, tmp_path: Path) -> None:
        """Works when selected_gpu was initially None (no GPU at startup)."""
        gpu1 = _gpu(1, GPU_B, used=100)
        ctx = self._make_ctx(
            tmp_path, selected_gpu=None, use_docker=False, telemetry=FakeGpuTelemetry([gpu1])
        )

        def case() -> None:
            ctx.device.reselect()
            assert ctx.device.monitor is not None
            ctx.device.monitor.stop()

        ctx.threads.run(case)

        assert ctx.device.selected_device is gpu1
        assert cast("LocalShellRunner", ctx.implementer_backend).env["CUDA_VISIBLE_DEVICES"] == "1"
