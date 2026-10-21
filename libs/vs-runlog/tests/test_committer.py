"""Commit scheduling and I/O isolation across generated thread interleavings."""

from __future__ import annotations

import os
import runpy
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_proto.api import RUN_LOG_MAGIC, Keyframe, LegacyEvent, Oneof, Record, RunHead
from vs_runlog.api import FileRunLogWriter, OsFileSync, RunLogReader, encode_frame, fold_skeleton
from vs_sim.api.testing import HANG_GUARD_S, SimThreads, VirtualClock

_FIXTURES = Path(__file__).resolve().parents[3] / "proto/testdata/skeleton"
_INTERVAL = 0.1


def declarations() -> list[Record]:
    return runpy.run_path(str(_FIXTURES / "generate.py"))["scenarios"]()["core"][0][:2]


def event(seq: int, epoch: int = 1) -> Record:
    return Record(seq=seq, epoch=epoch, body=Oneof("legacy_event", LegacyEvent(json=str(seq))))


def head(root: Path) -> int:
    path = root / "run.head"
    return RunHead.from_binary(path.read_bytes()).committed_seq if path.exists() else 0


class SyncProbe:
    """Real fsync with simulated pacing and the shared host fault mechanism.

    A held call is released by the test, never by a timeout. FaultGate owns failure
    scheduling; the probe only observes public files and delays the injected I/O.
    """

    def __init__(self, root: Path, threads: SimThreads) -> None:
        self.root = root
        self.threads = threads
        self.entered = threads.event()
        self.release = threads.event()
        self.hold_after: int | None = None
        self.log_delay = 0.0
        self.synced_seq = 0
        self.publications: list[tuple[int, float]] = []
        self.gate = FaultGate(FaultPlan(seed=0))
        self.real = OsFileSync()

    def __call__(self, descriptor: int, /) -> None:
        is_log = os.fstat(descriptor).st_ino == (self.root / "run.log").stat().st_ino
        if self.hold_after is not None:
            if self.hold_after == 0:
                self.hold_after = None
                self.entered.set()
                assert self.release.wait(HANG_GUARD_S)
            else:
                self.hold_after -= 1
        if is_log and self.log_delay:
            self.threads.sleep(self.log_delay)
        self.gate.around(Boundary.DURABLE_WRITE, "fsync", partial(self.real, descriptor))
        if is_log:
            with (self.root / "run.log").open("rb") as stream:
                records = list(RunLogReader(stream))
            self.synced_seq = records[-1].seq if records else 0
        visible = head(self.root)
        assert visible <= self.synced_seq
        if visible and (not self.publications or visible > self.publications[-1][0]):
            self.publications.append((visible, self.threads.clock.now()))

    def hold(self, skip: int = 0) -> None:
        self.entered.clear()
        self.release.clear()
        self.hold_after = skip

    def fail(self, skip: int) -> None:
        self.gate.plan = FaultPlan(
            seed=0,
            rules=(
                FaultRule(
                    boundary=Boundary.DURABLE_WRITE,
                    target="fsync",
                    at=len(self.gate.calls) + skip + 1,
                    fault=HostFault.CRASH_AFTER,
                ),
            ),
        )


@pytest.mark.regression
@given(skip=st.integers(0, 2), count=st.integers(1, 40), seed=st.integers(0, 2**32 - 1))
@example(skip=0, count=10, seed=0)
def test_appends_complete_while_fsync_or_head_publication_is_held(
    skip: int, count: int, seed: int
) -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        clock = VirtualClock()
        threads = SimThreads(clock, schedule_seed=seed)
        probe = SyncProbe(root, threads)

        def exercise() -> None:
            with FileRunLogWriter(
                root / "run.log", clock=clock, threads=threads, sync=probe
            ) as writer:
                for record in declarations():
                    writer.append(record)
                # Ordinary commit: log fsync, head-file fsync, head-directory fsync.
                probe.hold(skip)
                writer.append(event(3))
                assert probe.entered.wait(HANG_GUARD_S)
                before = clock.now()
                for seq in range(4, count + 4):
                    writer.append(event(seq))
                assert clock.now() == before
                # The completion of these appends precedes the release of commit I/O.
                assert not probe.release.is_set()
                assert head(root) in {2, 3}
                probe.release.set()
                writer.commit()
                assert head(root) == count + 3

        threads.run(exercise)


_OPERATIONS = st.lists(
    st.sampled_from(["append", "advance", "commit", "checkpoint", "lifecycle", "epoch"]),
    max_size=20,
)


@pytest.mark.regression
@given(
    operations=_OPERATIONS,
    delay=st.sampled_from([0.0, 0.03, 0.1, 0.2, 1.1]),
    seed=st.integers(0, 2**32 - 1),
    failure=st.integers(0, 2),
)
@example(
    operations=["append", "advance", "append", "commit", "epoch", "lifecycle"],
    delay=1.1,
    seed=0,
    failure=0,
)
@example(operations=["append", "advance", "advance", "append"], delay=0.2, seed=7, failure=2)
def test_interleavings_bound_lag_preserve_fence_and_surface_storage_failure(
    operations: list[str], delay: float, seed: int, failure: int
) -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        clock = VirtualClock()
        threads = SimThreads(clock, schedule_seed=seed)
        probe = SyncProbe(root, threads)

        def exercise() -> None:
            writer = FileRunLogWriter(root / "run.log", clock=clock, threads=threads, sync=probe)
            for record in declarations():
                writer.append(record)
            probe.log_delay = delay
            appended: list[tuple[int, float]] = []
            for operation in operations:
                if operation == "append":
                    seq = writer.sequence + 1
                    at = clock.now()
                    writer.append(event(seq, writer.epoch))
                    appended.append((seq, at))
                    assert clock.now() == at
                elif operation == "advance":
                    # Blocking work on the owner thread, not an asyncio yield.
                    threads.sleep(0.07)
                elif operation in {"commit", "checkpoint"}:
                    boundary = writer.commit if operation == "commit" else writer.checkpoint
                    boundary()
                    assert head(root) == writer.sequence
                else:
                    template = declarations()[0 if operation == "epoch" else 1]
                    template.seq = writer.sequence + 1
                    template.epoch = writer.epoch + (operation == "epoch")
                    if template.body is not None and template.body.field == "epoch_started":
                        template.body.value.epoch = template.epoch
                    writer.append(template)
                    assert head(root) == template.seq
                    assert (root / f"keyframes/{template.seq}.pb").exists()
                assert head(root) <= probe.synced_seq <= writer.sequence
            # Let natural deadlines finish, without commit() pulling them forward.
            threads.sleep(_INTERVAL + 2 * delay + 0.01)
            assert head(root) == writer.sequence
            for seq, at in appended:
                published_at = next(when for fence, when in probe.publications if fence >= seq)
                # A pending append can overlap one in-flight commit and then its
                # own. With no prior commit the tighter bound is interval + delay.
                assert published_at - at <= max(_INTERVAL, delay) + delay + 1e-8
            probe.fail(failure)
            writer.append(event(writer.sequence + 1, writer.epoch))
            threads.sleep(_INTERVAL + delay + 0.01)
            with pytest.raises(HostCrashError):
                writer.append(event(writer.sequence + 1, writer.epoch))
            with pytest.raises(HostCrashError):
                writer.close()
            # Failure released the file and process lock. Recovery must sync the
            # surviving log before publishing a fence for it.
            probe.gate.heal()
            probe.log_delay = 0.0
            with FileRunLogWriter(
                root / "run.log", clock=clock, threads=threads, sync=probe
            ) as recovered:
                assert head(root) == recovered.sequence
                keyframe = Keyframe.from_binary(
                    (root / f"keyframes/{recovered.sequence}.pb").read_bytes()
                )
                assert keyframe.through_seq == recovered.sequence

        threads.run(lambda: threads.run_async(_on_owner_loop(exercise)))


async def _on_owner_loop(exercise: Callable[[], None]) -> None:
    # This helper is an owner event loop occupied by synchronous work throughout.
    exercise()


@pytest.mark.parametrize("observed", [False, True])
def test_file_sync_keeps_descriptor_ownership_and_reports_os_errors(
    tmp_path: Path, *, observed: bool
) -> None:
    clock = VirtualClock()
    threads = SimThreads(clock)
    sync = SyncProbe(tmp_path, threads) if observed else OsFileSync()
    with (tmp_path / "run.log").open("w+b") as stream:
        stream.write(b"data")
        stream.flush()
        sync(stream.fileno())
        assert not stream.closed
    with pytest.raises((OSError, ValueError), match="descriptor"):
        sync(-1)


@given(delay=st.sampled_from([0.0, 0.03, 0.1, 0.2, 1.1]))
def test_idle_append_completes_within_interval_plus_one_commit(delay: float) -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        clock = VirtualClock()
        threads = SimThreads(clock)
        probe = SyncProbe(root, threads)

        def exercise() -> None:
            with FileRunLogWriter(
                root / "run.log", clock=clock, threads=threads, sync=probe
            ) as writer:
                for record in declarations():
                    writer.append(record)
                probe.log_delay = delay
                at = clock.now()
                writer.append(event(3))
                threads.sleep(_INTERVAL + delay + 0.01)
                assert head(root) == 3
                assert probe.publications[-1] == (3, pytest.approx(at + _INTERVAL + delay))

        threads.run(exercise)


def test_checkpoint_requested_during_commit_preempts_the_next_batch_deadline(
    tmp_path: Path,
) -> None:
    clock = VirtualClock()
    threads = SimThreads(clock)
    probe = SyncProbe(tmp_path, threads)
    started = threads.event()
    done = threads.event()

    def exercise() -> None:
        with FileRunLogWriter(
            tmp_path / "run.log", clock=clock, threads=threads, sync=probe
        ) as writer:
            for record in declarations():
                writer.append(record)
            probe.hold()
            writer.append(event(3))
            assert probe.entered.wait(HANG_GUARD_S)

            def checkpoint() -> None:
                started.set()
                writer.checkpoint()
                done.set()

            # The unseeded simulator runs this child until it waits, so the
            # checkpoint targets seq 3 while that batch is still in flight.
            child = threads.spawn(checkpoint, name="checkpoint")
            assert started.wait(HANG_GUARD_S)
            writer.append(event(4))
            assert not done.is_set()
            at = clock.now()
            probe.release.set()
            assert done.wait(HANG_GUARD_S)
            assert clock.now() == at
            assert head(tmp_path) == 4
            child.join(HANG_GUARD_S)

    threads.run(exercise)


@pytest.mark.parametrize("kind", ["lifecycle", "epoch"])
def test_a_boundary_keeps_its_keyframe_when_later_records_share_the_batch(
    tmp_path: Path, kind: str
) -> None:
    clock = VirtualClock()
    threads = SimThreads(clock)
    probe = SyncProbe(tmp_path, threads)
    started = threads.event()
    done = threads.event()
    expected = declarations()
    ending = declarations()[0 if kind == "epoch" else 1]
    ending.seq = 4
    if kind == "epoch":
        ending.epoch = 2
        assert ending.body is not None
        assert ending.body.field == "epoch_started"
        ending.body.value.epoch = 2
    expected.extend([event(3), ending, event(5, ending.epoch)])

    def exercise() -> None:
        with FileRunLogWriter(
            tmp_path / "run.log", clock=clock, threads=threads, sync=probe
        ) as writer:
            for record in expected[:2]:
                writer.append(record)
            probe.hold()
            writer.append(expected[2])
            assert probe.entered.wait(HANG_GUARD_S)

            def boundary() -> None:
                started.set()
                writer.append(ending)
                done.set()

            child = threads.spawn(boundary, name="boundary")
            assert started.wait(HANG_GUARD_S)
            writer.append(expected[4])
            assert not done.is_set()
            probe.release.set()
            assert done.wait(HANG_GUARD_S)
            offset = len(RUN_LOG_MAGIC) + sum(len(encode_frame(record)) for record in expected[:4])
            checkpoint = Keyframe.from_binary((tmp_path / "keyframes/4.pb").read_bytes())
            assert checkpoint == fold_skeleton(expected[:4], log_offset=offset)
            assert head(tmp_path) == 5
            child.join(HANG_GUARD_S)

    threads.run(exercise)
