"""Host capture teardown contracts driven by process events and a logical clock."""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st
from resources.profilers.rocprof import capture

runtime = capture.capture_runtime


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class ScriptedCaptureProcessGroup:
    """A profiler parent flushes while a signal-resistant child keeps it alive."""

    def __init__(
        self, directory: Path, clock: FakeClock, *, flush_at: int, writers: int, fault: str = ""
    ) -> None:
        self.directory = directory
        self.clock = clock
        self.flush_at = flush_at
        self.writers = writers
        self.fault = fault
        self.parent_alive = self.child_alive = True
        self.signals: list[str] = []
        self.flushed = False
        self.directory.mkdir(parents=True, exist_ok=True)
        self.process_root = self.directory / "proc"
        for pid in range(100, 100 + writers):
            process_dir = self.process_root / str(pid)
            process_dir.mkdir(parents=True, exist_ok=True)
            (process_dir / "maps").write_text("librocprofiler-sdk-tool.so\n")
        (self.directory / "target.log").write_text("")

    def members(self) -> set[int]:
        return set(range(100, 100 + self.writers)) if self.child_alive else set()

    def poll(self) -> int | None:
        return None if self.parent_alive or self.child_alive else -signal.SIGTERM

    def stop(self, signal_name: str) -> None:
        self.signals.append(signal_name)

    def wait(self, timeout_s: float) -> int | None:
        self.clock.now += timeout_s
        if self.clock.now >= self.flush_at and not self.flushed:
            self.flush()
        return self.poll()

    def flush(self) -> None:
        self.flushed = True
        markers = []
        for pid in range(100, 100 + self.writers):
            content = "Kernel_Name,Dispatch_Id,Start_Timestamp,End_Timestamp\nGEMM,1,100,200\n"
            if self.fault == "header_only":
                content = content.splitlines()[0] + "\n"
            elif self.fault == "truncated":
                content = content[:-1]
            elif self.fault == "bad_width":
                content += "incomplete,2\n"
            elif self.fault == "bad_timestamp":
                content = content.replace("100,200", "200,100")
            (self.directory / f"{pid}_kernel_trace.csv").write_text(content)
            if self.fault != "missing_marker":
                markers.extend(
                    [
                        "[rocprofv3] output generation :: 4.0 sec",
                        "[rocprofv3] tool finalization :: 4.1 sec",
                        f"[PPID=99][PID={pid}][rocprofv3_error_signal_handler] "
                        "found chained signal handler for 2... executing chained sigaction (SIGINFO)",
                    ]
                )
        (self.directory / "target.log").write_text("\n".join(markers) + "\n")

    def cleanup(self) -> None:
        self.signals.extend(["SIGTERM", "SIGKILL"])
        self.parent_alive = self.child_alive = False


@given(flush_at=st.integers(min_value=1, max_value=20), writers=st.integers(1, 4))
def test_finalized_trace_ends_grace_while_child_survives(flush_at: int, writers: int) -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        clock = FakeClock()
        group = ScriptedCaptureProcessGroup(directory, clock, flush_at=flush_at, writers=writers)
        result = runtime.stop_capture(
            group,
            runtime.Lifecycle(command="server", grace_s=120),
            completion=capture.RocprofTraceCompletion(directory, process_root=group.process_root),
            monotonic=clock.monotonic,
        )
        assert result.trace_complete
        assert result.escalated
        assert clock.now == flush_at
        assert not group.parent_alive
        assert not group.child_alive
        assert group.signals == ["SIGINT", "SIGTERM", "SIGKILL"]
        runtime.write_manifest(directory, {"kind": "timeline", "status": "ok"})
        report = capture.summary(str(directory))
        assert "GEMM" in report


@pytest.mark.parametrize(
    "fault", ["header_only", "truncated", "bad_width", "bad_timestamp", "missing_marker"]
)
def test_unproven_trace_retains_full_grace(tmp_path: Path, fault: str) -> None:
    clock = FakeClock()
    group = ScriptedCaptureProcessGroup(tmp_path, clock, flush_at=4, writers=1, fault=fault)
    result = runtime.stop_capture(
        group,
        runtime.Lifecycle(command="server", grace_s=120),
        completion=capture.RocprofTraceCompletion(tmp_path, process_root=group.process_root),
        monotonic=clock.monotonic,
    )
    assert not result.trace_complete
    assert clock.now == 120
    assert not group.child_alive


def test_each_trace_writer_must_finalize_after_stop(tmp_path: Path) -> None:
    clock = FakeClock()
    group = ScriptedCaptureProcessGroup(tmp_path, clock, flush_at=4, writers=2)
    group.flush()
    completion = capture.RocprofTraceCompletion(tmp_path, process_root=group.process_root)
    completion.begin(group.members())
    assert not completion.complete(
        group.members()
    )  # Old startup/helper finalizations do not count.
    group.flushed = False
    group.flush()
    # Append new stop-time output after the checkpoint.
    log = tmp_path / "target.log"
    log.write_text(log.read_text() * 2)
    assert completion.complete(group.members())
    with log.open("a") as handle:
        handle.write("[rocprofv3] tool finalization :: 4.1 sec\n")
    (tmp_path / "102_kernel_trace.csv").write_text(
        "Kernel_Name,Dispatch_Id,Start_Timestamp,End_Timestamp\nGEMM,1,100,200\n"
    )
    assert not completion.complete(group.members())


def test_requested_hip_trace_is_required_for_completion(tmp_path: Path) -> None:
    clock = FakeClock()
    group = ScriptedCaptureProcessGroup(tmp_path, clock, flush_at=4, writers=1)
    completion = capture.RocprofTraceCompletion(
        tmp_path, hip_api=True, process_root=group.process_root
    )
    completion.begin(group.members())
    group.flush()
    assert not completion.complete(group.members())
    (tmp_path / "100_hip_api_trace.csv").write_text(
        "Name,Start_Timestamp,End_Timestamp\nhipLaunchKernel,100,200\n"
    )
    assert completion.complete(group.members())


class ScriptedWrapperProcess:
    """Popen contract for the old wrapper-only grace, with logical timed waits."""

    # No OS process exists. The old signal sender suppresses ProcessLookupError.
    pid = 2_147_483_647

    def __init__(self, group: ScriptedCaptureProcessGroup) -> None:
        self.group = group
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, *, timeout: float) -> int:
        self.group.wait(timeout)
        # The wrapper eventually exits, but the complete trace was available
        # much earlier. A successful late exit still incurs the bug's cost.
        if self.group.clock.now >= 120:
            self.returncode = 0
            return 0
        raise subprocess.TimeoutExpired("scripted-wrapper", timeout)


def test_complete_capture_does_not_wait_for_wrapper_exit(tmp_path: Path) -> None:
    """Base-compatible regression: observe the old grace through timed waits."""
    clock = FakeClock()
    group = ScriptedCaptureProcessGroup(tmp_path, clock, flush_at=5, writers=1)
    lifecycle = runtime.Lifecycle(command="server", grace_s=120)
    if hasattr(runtime, "stop_capture"):
        runtime.stop_capture(
            group,
            lifecycle,
            completion=capture.RocprofTraceCompletion(tmp_path, process_root=group.process_root),
            monotonic=clock.monotonic,
        )
    else:
        # test-isolation: the old teardown has no public injected-clock seam.
        # A subprocess test would depend on real grace time; patching its clock
        # would couple the test to globals. Replay its Popen contract directly.
        runtime._stop_and_wait_grace(  # noqa: SLF001  # LW-940501; the public clock seam did not exist at the base, and real-time waits or patched globals would undermine this deterministic regression.
            ScriptedWrapperProcess(group), lifecycle, None
        )
    assert clock.now <= group.flush_at + 1
    assert (tmp_path / "100_kernel_trace.csv").read_text().endswith("GEMM,1,100,200\n")


def test_finalized_trace_cleans_child_after_parent_exits(tmp_path: Path) -> None:
    clock = FakeClock()
    group = ScriptedCaptureProcessGroup(tmp_path, clock, flush_at=4, writers=1)
    group.parent_alive = False
    result = runtime.stop_capture(
        group,
        runtime.Lifecycle(command="server", grace_s=120),
        completion=capture.RocprofTraceCompletion(tmp_path, process_root=group.process_root),
        monotonic=clock.monotonic,
    )
    assert result.trace_complete
    assert clock.now == 4
    assert not group.child_alive


def test_live_r23_finalization_format_identifies_writer(tmp_path: Path) -> None:
    clock = FakeClock()
    group = ScriptedCaptureProcessGroup(tmp_path, clock, flush_at=4, writers=1)
    process_dir = group.process_root / "293661"
    process_dir.mkdir()
    (process_dir / "maps").write_text("librocprofiler-sdk-tool.so\n")
    completion = capture.RocprofTraceCompletion(tmp_path, process_root=group.process_root)
    completion.begin({293661})
    (tmp_path / "target.log").write_text(
        "W20261004 07:03:37.764042 140375033667200 simple_timer.cpp:55] "
        "[rocprofv3] output generation ::     4.006442 sec\n"
        "W20261004 07:03:37.769951 140375033667200 simple_timer.cpp:55] "
        "[rocprofv3] tool finalization ::     4.012697 sec\n"
        "W20261004 07:03:37.770451 140375033667200 tool.cpp:3184] "
        "[PPID=293657][PID=293661][TID=293661][rocprofv3_error_signal_handler] "
        "rocprofv3 found chained signal handler for 2... executing chained sigaction (SIGINFO)\n"
    )
    (tmp_path / "293661_kernel_trace.csv").write_text(
        "Kernel_Name,Dispatch_Id,Start_Timestamp,End_Timestamp\nGEMM,1,100,200\n"
    )
    assert completion.complete({293661})


def test_traced_child_without_csv_blocks_early_escalation(tmp_path: Path) -> None:
    clock = FakeClock()
    group = ScriptedCaptureProcessGroup(tmp_path, clock, flush_at=4, writers=2)
    completion = capture.RocprofTraceCompletion(tmp_path, process_root=group.process_root)
    completion.begin(group.members())
    group.flush()
    child_trace = tmp_path / "101_kernel_trace.csv"
    child_content = child_trace.read_text()
    child_trace.unlink()
    log = tmp_path / "target.log"
    parent_markers, child_markers = log.read_text().split(
        "[rocprofv3] output generation :: 4.0 sec", maxsplit=2
    )[1:]
    log.write_text("[rocprofv3] output generation :: 4.0 sec" + parent_markers)
    assert not completion.complete(group.members())
    # Merely catching SIGINT is before finalize_rocprofv3, not a flush proof.
    with log.open("a") as handle:
        handle.write("[PID=101][rocprofv3_error_signal_handler] caught signal 2...\n")
    assert not completion.complete(group.members())
    child_trace.write_text(child_content)
    with log.open("a") as handle:
        handle.write("[rocprofv3] output generation :: 4.0 sec" + child_markers)
    assert completion.complete(group.members())


@pytest.mark.parametrize("fault", ["missing", "non_utf8"])
def test_unreadable_writer_inventory_keeps_full_grace(tmp_path: Path, fault: str) -> None:
    clock = FakeClock()
    group = ScriptedCaptureProcessGroup(tmp_path, clock, flush_at=4, writers=1)
    maps = group.process_root / "100" / "maps"
    if fault == "missing":
        maps.unlink()
    else:
        maps.write_bytes(b"\xff librocprofiler-sdk-tool.so")
    result = runtime.stop_capture(
        group,
        runtime.Lifecycle(command="server", grace_s=120),
        completion=capture.RocprofTraceCompletion(tmp_path, process_root=group.process_root),
        monotonic=clock.monotonic,
    )
    assert not result.trace_complete
    assert clock.now == 120


@given(
    pid=st.integers(min_value=1, max_value=1_000_000),
    original_birth=st.integers(min_value=0, max_value=1_000_000),
    replacement_birth=st.integers(min_value=0, max_value=1_000_000),
)
def test_cleanup_ownership_requires_same_process_birth(
    pid: int, original_birth: int, replacement_birth: int
) -> None:
    original = runtime.ProcessIdentity(pid, original_birth)
    replacement = runtime.ProcessIdentity(pid, replacement_birth)
    owned = runtime.owned_process_ids({original}, {replacement})
    assert owned == ({pid} if original_birth == replacement_birth else set())
