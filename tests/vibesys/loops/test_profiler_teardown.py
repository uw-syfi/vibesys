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


def write_birth(directory: Path, pid: int, birth: int) -> None:
    """Minimal Linux stat fixture with starttime at field 22."""
    (directory / "stat").write_text(f"{pid} (writer) S 99 99 99 " + "0 " * 15 + f"{birth}\n")


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
            write_birth(process_dir, pid, pid)
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

    def history_complete(self) -> bool:
        return True

    def quiesce(self) -> set[int] | None:
        return self.members()

    def resume(self) -> None:
        pass

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
    write_birth(process_dir, 293661, 293661)
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


class LateWriterGroup(ScriptedCaptureProcessGroup):
    """A writer forks after inventory, before proof evaluation or its fence."""

    def __init__(
        self, directory: Path, clock: FakeClock, *, birth_phase: int, finalize: bool
    ) -> None:
        super().__init__(directory, clock, flush_at=1, writers=1)
        self.birth_phase = birth_phase
        self.finalize = finalize
        self.inventory_calls = 0
        self.birth_seen = False
        self.fenced = False

    def birth(self) -> None:
        self.birth_seen = True
        self.writers = 2
        directory = self.process_root / "101"
        directory.mkdir(exist_ok=True)
        (directory / "maps").write_text("librocprofiler-sdk-tool.so\n")
        write_birth(directory, 101, 101)
        if self.finalize:
            self.flush()

    def members(self) -> set[int]:
        snapshot = super().members()
        self.inventory_calls += 1
        if self.flushed and not self.birth_seen and self.birth_phase == 0:
            self.birth()
        return snapshot

    def quiesce(self) -> set[int] | None:
        if not self.birth_seen and self.birth_phase == 1:
            self.birth()
        self.fenced = True
        return super().members()

    def resume(self) -> None:
        self.fenced = False


@given(birth_phase=st.integers(0, 1), finalize=st.booleans())
def test_writer_born_between_inventory_and_cleanup_retains_grace(
    birth_phase: int, *, finalize: bool
) -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        clock = FakeClock()
        group = LateWriterGroup(directory, clock, birth_phase=birth_phase, finalize=finalize)
        result = runtime.stop_capture(
            group,
            runtime.Lifecycle(command="server", grace_s=120),
            completion=capture.RocprofTraceCompletion(directory, process_root=group.process_root),
            monotonic=clock.monotonic,
        )
        assert group.birth_seen
        assert not result.trace_complete or group.finalize
        if not group.finalize:
            assert clock.now == 120
        assert not group.fenced


@given(pid=st.integers(100, 1_000_000), birth=st.integers(0, 1_000_000), delta=st.integers(1, 1000))
def test_reused_writer_pid_cannot_reuse_finalization(pid: int, birth: int, delta: int) -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        process_dir = directory / "proc" / str(pid)
        process_dir.mkdir(parents=True)
        (process_dir / "maps").write_text("librocprofiler-sdk-tool.so\n")
        write_birth(process_dir, pid, birth)
        log = directory / "target.log"
        log.write_text("")
        completion = capture.RocprofTraceCompletion(directory, process_root=process_dir.parent)
        completion.begin({pid})
        log.write_text(
            "[rocprofv3] output generation :: 4 sec\n"
            "[rocprofv3] tool finalization :: 4 sec\n"
            f"[PID={pid}][rocprofv3_error_signal_handler] executing chained sigaction\n"
        )
        (directory / f"{pid}_kernel_trace.csv").write_text(
            "Kernel_Name,Dispatch_Id,Start_Timestamp,End_Timestamp\nGEMM,1,100,200\n"
        )
        assert completion.complete({pid})
        write_birth(process_dir, pid, birth + delta)
        assert not completion.complete({pid})


class FakeProcessTable:
    """Process tree with identity-checked signals and deterministic fork events."""

    def __init__(self, parents: list[int], *, stop_delay: int = 0) -> None:
        self.parents = {index + 10: 10 + parent for index, parent in enumerate(parents, 1)}
        self.parents[10] = 1
        self.births = {pid: runtime.ProcessIdentity(pid, pid * 10) for pid in self.parents}
        self.live = set(self.parents)
        self.history = set(self.births.values())
        self.detached: set[int] = set()
        self.stopped_pids: set[int] = set()
        self.pending_stops: dict[int, int] = {}
        self.stop_delay = stop_delay
        self.signals: list[tuple[runtime.ProcessIdentity, signal.Signals]] = []

    def birth(self, pid: int, parent: int) -> None:
        self.parents[pid] = parent
        self.births[pid] = runtime.ProcessIdentity(pid, pid * 10)
        self.live.add(pid)
        self.history.add(self.births[pid])
        self.detached.add(pid)

    def history_complete(self, observed: set[runtime.ProcessIdentity]) -> bool:
        return self.history <= observed

    def snapshot(self) -> dict[int, runtime.ProcessSnapshot]:
        for pid in tuple(self.pending_stops):
            self.pending_stops[pid] -= 1
            if self.pending_stops[pid] <= 0:
                self.stopped_pids.add(pid)
                del self.pending_stops[pid]
        return {
            pid: runtime.ProcessSnapshot(
                self.births[pid],
                self.parents[pid],
                pid if pid in self.detached else 10,
                "T" if pid in self.stopped_pids else "S",
            )
            for pid in self.live
        }

    def signal(self, identity: runtime.ProcessIdentity, sig: signal.Signals) -> None:
        if identity.pid not in self.live or self.births[identity.pid] != identity:
            return
        self.signals.append((identity, sig))
        if sig == signal.SIGKILL:
            self.live.discard(identity.pid)
        elif sig == signal.SIGSTOP:
            self.pending_stops.setdefault(identity.pid, self.stop_delay)
        elif sig == signal.SIGCONT:
            self.stopped_pids.discard(identity.pid)
            self.pending_stops.pop(identity.pid, None)

    def wait_for_death(self, pids: set[int], timeout_s: float) -> bool:
        if timeout_s < 0:
            raise ValueError(timeout_s)
        return not bool(pids & self.live)


class ForkingWrapperProcess:
    """Popen wait starts a detached descendant during the TERM escalation wait."""

    pid = 10

    def __init__(self, table: FakeProcessTable, parent: int) -> None:
        self.table = table
        self.parent = parent
        self.forked = False

    def poll(self) -> int | None:
        return None if self.pid in self.table.live else -signal.SIGKILL

    def wait(self, *, timeout: float) -> int:
        if self.pid not in self.table.live:
            return -signal.SIGKILL
        if not self.forked:
            self.forked = True
            self.table.birth(1000, self.parent)
        raise subprocess.TimeoutExpired("forking-wrapper", timeout)


@st.composite
def process_trees(draw: st.DrawFn) -> list[int]:
    count = draw(st.integers(1, 8))
    return [draw(st.integers(0, index - 1)) for index in range(1, count + 1)]


@given(parents=process_trees(), parent_index=st.integers(0, 8))
def test_cleanup_rediscovers_detached_descendants_after_term(
    parents: list[int], parent_index: int
) -> None:
    table = FakeProcessTable(parents)
    parent = 10 + parent_index % (len(parents) + 1)
    proc = ForkingWrapperProcess(table, parent)
    group = runtime.SubprocessCaptureProcessGroup(proc, table=table)
    original = set(table.births.values())
    # Reuse a former child's PID for an unrelated process outside the capture.
    replaced = 10 + len(parents)
    table.parents[replaced] = 1
    table.detached.add(replaced)
    table.births[replaced] = runtime.ProcessIdentity(replaced, 99999)
    if parent == replaced:
        proc.parent = 10
    group.cleanup()
    assert table.live == {replaced}
    assert all(identity in original or identity.pid == 1000 for identity, _ in table.signals)
    assert (runtime.ProcessIdentity(1000, 10000), signal.SIGKILL) in table.signals


@given(parents=process_trees(), stop_delay=st.integers(0, 8))
def test_process_fence_requires_acknowledged_stops_and_resumes(
    parents: list[int], stop_delay: int
) -> None:
    table = FakeProcessTable(parents, stop_delay=stop_delay)
    group = runtime.SubprocessCaptureProcessGroup(ForkingWrapperProcess(table, 10), table=table)
    fenced = group.quiesce()
    assert fenced == table.live
    assert table.stopped_pids == table.live
    group.resume()
    assert not table.stopped_pids
    assert not table.pending_stops


def test_failed_fence_releases_pending_stops() -> None:
    table = FakeProcessTable([0], stop_delay=1000)
    group = runtime.SubprocessCaptureProcessGroup(ForkingWrapperProcess(table, 10), table=table)
    assert group.quiesce() is None
    group.resume()
    assert not table.stopped_pids
    assert not table.pending_stops


class ProofWrapperProcess:
    """Logical Popen waits for tests exercising the production group owner."""

    pid = 10

    def __init__(self, table: FakeProcessTable, clock: FakeClock) -> None:
        self.table = table
        self.clock = clock

    def poll(self) -> int | None:
        return None if self.pid in self.table.live else -signal.SIGKILL

    def wait(self, *, timeout: float) -> int:
        if self.pid not in self.table.live:
            return -signal.SIGKILL
        self.clock.now += timeout
        raise subprocess.TimeoutExpired("proof-wrapper", timeout)


class ReusingWriterTable(FakeProcessTable):
    def __init__(self, directory: Path, *, during_fence: bool, birth: int) -> None:
        super().__init__([0])
        self.directory = directory
        self.during_fence = during_fence
        self.birth_tick = birth
        self.reused = False
        self.history.discard(self.births[11])
        self.births[11] = runtime.ProcessIdentity(11, birth)
        self.history.add(self.births[11])
        self.process_dir = directory / "proc" / "11"
        self.process_dir.mkdir(parents=True)
        wrapper_dir = directory / "proc" / "10"
        wrapper_dir.mkdir()
        (wrapper_dir / "maps").write_text("")
        write_birth(wrapper_dir, 10, 100)
        (self.process_dir / "maps").write_text("librocprofiler-sdk-tool.so\n")
        write_birth(self.process_dir, 11, birth)
        (directory / "target.log").write_text("")

    def replace(self) -> None:
        self.reused = True
        self.births[11] = runtime.ProcessIdentity(11, self.birth_tick + 1)
        self.history.add(self.births[11])
        self.stopped_pids.discard(11)
        self.pending_stops.pop(11, None)
        write_birth(self.process_dir, 11, self.birth_tick + 1)

    def signal(self, identity: runtime.ProcessIdentity, sig: signal.Signals) -> None:
        if sig == signal.SIGINT:
            (self.directory / "target.log").write_text(
                "[rocprofv3] output generation :: 4 sec\n"
                "[rocprofv3] tool finalization :: 4 sec\n"
                "[PID=11][rocprofv3_error_signal_handler] executing chained sigaction\n"
            )
            (self.directory / "11_kernel_trace.csv").write_text(
                "Kernel_Name,Dispatch_Id,Start_Timestamp,End_Timestamp\nGEMM,1,100,200\n"
            )
            if not self.during_fence and not self.reused:
                self.replace()
        elif sig == signal.SIGSTOP and self.during_fence and not self.reused:
            self.replace()
        super().signal(identity, sig)


@given(during_fence=st.booleans(), birth=st.integers(0, 1_000_000))
def test_stop_capture_inventories_owned_pid_reuse(*, during_fence: bool, birth: int) -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        table = ReusingWriterTable(directory, during_fence=during_fence, birth=birth)
        clock = FakeClock()
        group = runtime.SubprocessCaptureProcessGroup(
            ProofWrapperProcess(table, clock), table=table
        )
        result = runtime.stop_capture(
            group,
            runtime.Lifecycle(command="server", grace_s=120),
            completion=capture.RocprofTraceCompletion(directory, process_root=directory / "proc"),
            monotonic=clock.monotonic,
        )
        assert not result.trace_complete
        assert clock.now >= 120
        assert not table.live
        assert not table.pending_stops


@given(writers=st.integers(2, 5), missing_index=st.integers(0, 4))
def test_finalized_writer_requires_its_own_complete_records(
    writers: int, missing_index: int
) -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        group = ScriptedCaptureProcessGroup(directory, FakeClock(), flush_at=1, writers=writers)
        completion = capture.RocprofTraceCompletion(directory, process_root=group.process_root)
        completion.begin(group.members())
        group.flush()
        missing = directory / f"{100 + missing_index % writers}_kernel_trace.csv"
        missing.unlink()
        assert not completion.complete(group.members())


class InterleavedWriterGroup(ScriptedCaptureProcessGroup):
    """Writers can be born, finalize, or exit without finalization at each tick."""

    def __init__(self, directory: Path, clock: FakeClock, events: list[tuple[str, int]]) -> None:
        super().__init__(directory, clock, flush_at=120, writers=1)
        self.events = list(events)
        self.live_writers = {100}
        self.ever_writers = {100}
        self.finalized: set[int] = set()

    def members(self) -> set[int]:
        return set(self.live_writers) if self.child_alive else set()

    def wait(self, timeout_s: float) -> int | None:
        self.clock.now += timeout_s
        if self.events:
            action, index = self.events.pop(0)
            pid = 100 + index
            if action == "birth" and pid not in self.ever_writers:
                self.ever_writers.add(pid)
                self.live_writers.add(pid)
                process_dir = self.process_root / str(pid)
                process_dir.mkdir(exist_ok=True)
                (process_dir / "maps").write_text("librocprofiler-sdk-tool.so\n")
                write_birth(process_dir, pid, pid)
            elif action == "finalize" and pid in self.live_writers:
                self.finalized.add(pid)
                with (self.directory / "target.log").open("a") as handle:
                    handle.write(
                        "[rocprofv3] output generation :: 4 sec\n"
                        "[rocprofv3] tool finalization :: 4 sec\n"
                        f"[PID={pid}][rocprofv3_error_signal_handler] executing chained sigaction\n"
                    )
                (self.directory / f"{pid}_kernel_trace.csv").write_text(
                    "Kernel_Name,Dispatch_Id,Start_Timestamp,End_Timestamp\nGEMM,1,100,200\n"
                )
            elif action == "exit":
                self.live_writers.discard(pid)
        return self.poll()


@given(
    events=st.lists(
        st.tuples(st.sampled_from(["birth", "finalize", "exit"]), st.integers(0, 4)), max_size=20
    )
)
def test_completion_implies_every_observed_writer_finalized(events: list[tuple[str, int]]) -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        clock = FakeClock()
        group = InterleavedWriterGroup(directory, clock, events)
        result = runtime.stop_capture(
            group,
            runtime.Lifecycle(command="server", grace_s=30),
            completion=capture.RocprofTraceCompletion(directory, process_root=group.process_root),
            monotonic=clock.monotonic,
        )
        assert not result.trace_complete or group.ever_writers <= group.finalized


class ProofReadError(OSError):
    """Injected completion read failure."""


class FailingFenceProof:
    """The second proof raises after the process group has been fenced."""

    def __init__(self) -> None:
        self.calls = 0

    def begin(self, process_ids: set[int]) -> None:
        if not process_ids:
            raise ValueError(process_ids)

    def complete(self, process_ids: set[int]) -> bool:
        self.calls += 1
        if self.calls > 1:
            raise ProofReadError
        return bool(process_ids)


def test_completion_exception_resumes_fenced_processes() -> None:
    table = FakeProcessTable([0])
    clock = FakeClock()
    group = runtime.SubprocessCaptureProcessGroup(ProofWrapperProcess(table, clock), table=table)
    with pytest.raises(ProofReadError):
        runtime.stop_capture(
            group,
            runtime.Lifecycle(command="server", grace_s=120),
            completion=FailingFenceProof(),
            monotonic=clock.monotonic,
        )
    assert not table.pending_stops
    assert not table.stopped_pids
    assert table.live == {10, 11}


class TransientWriterTable(ReusingWriterTable):
    """A detached writer is born and exits entirely between inventory scans."""

    def __init__(self, directory: Path, *, during_fence: bool, pid: int, birth: int) -> None:
        super().__init__(directory, during_fence=False, birth=110)
        self.transient_signal = signal.SIGSTOP if during_fence else signal.SIGINT
        self.transient_pid = pid
        self.transient_birth = birth

    def replace(self) -> None:
        # This scenario has an additional writer, not a reused original PID.
        pass

    def signal(self, identity: runtime.ProcessIdentity, sig: signal.Signals) -> None:
        if sig == self.transient_signal and not self.reused:
            self.reused = True
            self.birth(self.transient_pid, 11)
            self.history.discard(self.births[self.transient_pid])
            self.births[self.transient_pid] = runtime.ProcessIdentity(
                self.transient_pid, self.transient_birth
            )
            self.history.add(self.births[self.transient_pid])
            self.live.remove(self.transient_pid)
        super().signal(identity, sig)


@given(during_fence=st.booleans(), pid=st.integers(100, 1_000_000), birth=st.integers(0, 1_000_000))
def test_transient_unfinalized_writer_prevents_completion(
    *, during_fence: bool, pid: int, birth: int
) -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        table = TransientWriterTable(directory, during_fence=during_fence, pid=pid, birth=birth)
        clock = FakeClock()
        group = runtime.SubprocessCaptureProcessGroup(
            ProofWrapperProcess(table, clock), table=table
        )
        result = runtime.stop_capture(
            group,
            runtime.Lifecycle(command="server", grace_s=120),
            completion=capture.RocprofTraceCompletion(directory, process_root=directory / "proc"),
            monotonic=clock.monotonic,
        )
        assert not result.trace_complete
        assert clock.now >= 120


def test_polling_process_table_cannot_prove_writer_history() -> None:
    table = runtime.LinuxProcessTable()
    assert not table.history_complete(set())


class MissingWriterHistoryGroup(ScriptedCaptureProcessGroup):
    """A writer exits without artifacts before the next members observation."""

    def __init__(self, directory: Path, clock: FakeClock, *, hidden_pid: int) -> None:
        super().__init__(directory, clock, flush_at=120, writers=1)
        self.history = {100}
        self.observed: set[int] = set()
        self.hidden_pid = hidden_pid

    def members(self) -> set[int]:
        current = super().members()
        self.observed |= current
        return current

    def stop(self, signal_name: str) -> None:
        super().stop(signal_name)
        self.flush()
        self.history.add(self.hidden_pid)
        # This birth and exit are recorded in the continuous journal, but its
        # /proc entry, finalization marker and CSV vanish before the next scan.

    def history_complete(self) -> bool:
        return self.history <= self.observed


@given(hidden_pid=st.integers(101, 1_000_000))
def test_unobserved_exited_writer_retains_grace(hidden_pid: int) -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        clock = FakeClock()
        group = MissingWriterHistoryGroup(directory, clock, hidden_pid=hidden_pid)
        result = runtime.stop_capture(
            group,
            runtime.Lifecycle(command="server", grace_s=120),
            completion=capture.RocprofTraceCompletion(directory, process_root=group.process_root),
            monotonic=clock.monotonic,
        )
        assert not result.trace_complete
        assert clock.now == 120


@given(pid=st.integers(100, 1_000_000), birth=st.integers(0, 1_000_000))
def test_new_writer_marker_cannot_reuse_pre_stop_csv(pid: int, birth: int) -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        process_dir = directory / "proc" / str(pid)
        process_dir.mkdir(parents=True)
        (process_dir / "maps").write_text("librocprofiler-sdk-tool.so\n")
        write_birth(process_dir, pid, birth)
        csv_path = directory / f"{pid}_kernel_trace.csv"
        csv_path.write_text(
            "Kernel_Name,Dispatch_Id,Start_Timestamp,End_Timestamp\nGEMM,1,100,200\n"
        )
        (directory / "target.log").write_text("")
        completion = capture.RocprofTraceCompletion(directory, process_root=process_dir.parent)
        completion.begin({pid})
        (directory / "target.log").write_text(
            "[rocprofv3] output generation :: 4 sec\n"
            "[rocprofv3] tool finalization :: 4 sec\n"
            f"[PID={pid}][rocprofv3_error_signal_handler] executing chained sigaction\n"
        )
        assert not completion.complete({pid})
        csv_path.write_text(csv_path.read_text() + "GEMM,2,300,400\n")
        assert completion.complete({pid})


@given(pid=st.integers(101, 1_000_000))
def test_new_writer_disappearing_before_maps_read_keeps_proof_unproven(pid: int) -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        group = ScriptedCaptureProcessGroup(directory, FakeClock(), flush_at=1, writers=1)
        completion = capture.RocprofTraceCompletion(directory, process_root=group.process_root)
        completion.begin(group.members())
        group.flush()
        process_dir = group.process_root / str(pid)
        process_dir.mkdir()
        maps = process_dir / "maps"
        maps.write_text("librocprofiler-sdk-tool.so\n")
        write_birth(process_dir, pid, pid)
        snapshot = {100, pid}
        maps.unlink()
        (process_dir / "stat").unlink()
        process_dir.rmdir()
        assert not completion.complete(snapshot)
