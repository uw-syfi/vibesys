"""Group durability, timer ownership and crash recovery through public store APIs."""

from __future__ import annotations

import asyncio
import io
import runpy
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_proto.api import RUN_LOG_MAGIC, Keyframe, LegacyEvent, Oneof, Record, RunHead
from vs_runlog.api import FileRunLogWriter, RunLogReader, encode_frame, fold_skeleton
from vs_sim.api.testing import (
    ManualClock,
    SimBlockingRunner,
    SimThreads,
    VirtualClock,
    loop_threads,
    run_virtual,
)

_FIXTURES = Path(__file__).resolve().parents[3] / "proto/testdata/skeleton"


def initial_records() -> list[Record]:
    return runpy.run_path(str(_FIXTURES / "generate.py"))["scenarios"]()["core"][0][:2]


def event(seq: int) -> Record:
    return Record(seq=seq, epoch=1, body=Oneof("legacy_event", LegacyEvent(json=str(seq))))


def head(root: Path) -> int:
    return RunHead.from_binary((root / "run.head").read_bytes()).committed_seq


@pytest.mark.regression
@given(st.integers(min_value=1, max_value=300))
@example(count=300)
def test_appends_leave_visibility_and_keyframes_at_previous_commit(count: int) -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        with FileRunLogWriter(
            root / "run.log", clock=ManualClock(), threads=SimThreads()
        ) as writer:
            for record in initial_records():
                writer.append(record)
            before = {p.name: p.read_bytes() for p in (root / "keyframes").iterdir()}
            for seq in range(3, count + 3):
                writer.append(event(seq))
            assert writer.sequence == count + 2
            assert head(root) == 2
            assert {p.name: p.read_bytes() for p in (root / "keyframes").iterdir()} == before
            writer.commit()
            assert head(root) == count + 2
            if count >= 256:
                checkpoint = Keyframe.from_binary((root / f"keyframes/{count + 2}.pb").read_bytes())
                with (root / "run.log").open("rb") as stream:
                    assert checkpoint == fold_skeleton(
                        RunLogReader(stream), log_offset=stream.seek(0, 2)
                    )
            # An empty commit must perform no publication.
            (root / "run.head").unlink()
            (root / "run.head").mkdir()
            writer.commit()
            (root / "run.head").rmdir()


def test_first_pending_append_sets_deadline_without_debouncing(tmp_path: Path) -> None:
    clock = VirtualClock()

    async def exercise() -> None:
        with FileRunLogWriter(tmp_path / "run.log", clock=clock, threads=SimThreads()) as writer:
            for record in initial_records():
                writer.append(record)
            writer.append(event(3))
            await clock.sleep(0.06)
            assert head(tmp_path) == 2
            writer.append(event(4))
            await clock.sleep(0.05)
            assert head(tmp_path) == 4
            writer.append(event(5))
            await clock.sleep(0.11)
            assert head(tmp_path) == 5

    run_virtual(clock, exercise())


@pytest.mark.parametrize("boundary", ["commit", "lifecycle", "close"])
def test_explicit_boundary_invalidates_old_timer(tmp_path: Path, boundary: str) -> None:
    clock = VirtualClock()

    async def exercise() -> None:
        writer = FileRunLogWriter(tmp_path / "run.log", clock=clock, threads=SimThreads())
        templates = runpy.run_path(str(_FIXTURES / "generate.py"))["scenarios"]()["core"][0]
        for record in templates[:2]:
            writer.append(record)
        writer.append(event(3))
        await clock.sleep(0.06)
        if boundary == "lifecycle":
            ending = templates[-1]
            ending.seq = 4
            writer.append(ending)
            assert head(tmp_path) == 4
        elif boundary == "commit":
            writer.commit()
            assert head(tmp_path) == 3
        else:
            writer.close()
            assert head(tmp_path) == 3
        # A stale deadline must not touch projections after a boundary/close.
        (tmp_path / "run.head").unlink()
        (tmp_path / "run.head").mkdir()
        await clock.sleep(0.05)
        (tmp_path / "run.head").rmdir()
        writer.close()

    run_virtual(clock, exercise())


def test_deferred_failure_is_reported_and_prevents_further_appends(tmp_path: Path) -> None:
    clock = VirtualClock()

    async def exercise() -> None:
        writer = FileRunLogWriter(tmp_path / "run.log", clock=clock, threads=SimThreads())
        for record in initial_records():
            writer.append(record)
        writer.append(event(3))
        (tmp_path / "run.head").unlink()
        (tmp_path / "run.head").mkdir()
        await clock.sleep(0.11)
        with pytest.raises(OSError, match=r"run.head"):
            writer.append(event(4))
        with pytest.raises(OSError, match=r"run.head"):
            writer.close()
        (tmp_path / "run.head").rmdir()
        with FileRunLogWriter(
            tmp_path / "run.log", clock=ManualClock(), threads=SimThreads()
        ) as recovered:
            assert recovered.sequence == 3
            assert head(tmp_path) == 3

    run_virtual(clock, exercise())


@given(st.lists(st.tuples(st.integers(0, 4), st.integers(0, 2**32 - 1)), max_size=20))
@example(operations=[(0, 0), (1, 0), (0, 0), (2, 3), (0, 0), (3, 1)])
def test_commit_crash_recovery_preserves_durable_prefix(
    operations: list[tuple[int, int]],
) -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / "run.log"
        writer = FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads())
        expected = initial_records()
        for record in expected:
            writer.append(record)
        committed = len(expected)
        templates = runpy.run_path(str(_FIXTURES / "generate.py"))["scenarios"]()["core"][0]
        for operation, cut in operations:
            if operation in {0, 4}:
                record = (
                    event(len(expected) + 1)
                    if operation == 0
                    else Record.from_binary(templates[2 + cut % 7].to_binary())
                )
                record.seq = len(expected) + 1
                writer.append(record)
                expected.append(record)
            elif operation == 1:
                writer.commit()
                committed = len(expected)
            else:
                # Model host death: retain the durable prefix and any arbitrary
                # byte prefix of pending writes. close only releases the live
                # test handle; restore the crash image captured before close.
                durable = RUN_LOG_MAGIC + b"".join(map(encode_frame, expected[:committed]))
                pending = b"".join(map(encode_frame, expected[committed:]))
                suffix = pending[: cut % (len(pending) + 1)]
                crash_log = durable + suffix
                crash_head = (root / "run.head").read_bytes()
                checkpoints = {p.name: p.read_bytes() for p in (root / "keyframes").iterdir()}
                writer.close()
                path.write_bytes(crash_log)
                (root / "run.head").write_bytes(crash_head)
                for checkpoint in (root / "keyframes").iterdir():
                    checkpoint.unlink()
                for name, payload in checkpoints.items():
                    (root / "keyframes" / name).write_bytes(payload)
                reader = RunLogReader(io.BytesIO(crash_log))
                surviving = list(reader)
                assert surviving[:committed] == expected[:committed]
                writer = FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads())
                expected = surviving
                committed = len(expected)
                checkpoint = Keyframe.from_binary((root / f"keyframes/{committed}.pb").read_bytes())
                assert checkpoint == fold_skeleton(expected, log_offset=path.stat().st_size)
            assert head(root) == committed
            assert (
                list(RunLogReader(io.BytesIO(path.read_bytes())))[:committed]
                == expected[:committed]
            )
        writer.close()
        assert head(root) == len(expected)


def test_recovery_refuses_to_erase_a_committed_record(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()) as writer:
        for record in initial_records():
            writer.append(record)
    before = (tmp_path / "run.head").read_bytes()
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError, match="lost committed records"):
        FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads())
    assert (tmp_path / "run.head").read_bytes() == before


def test_off_loop_construction_and_appends_use_the_supplied_loop(tmp_path: Path) -> None:
    clock = VirtualClock()

    async def exercise() -> None:
        threads = loop_threads()
        runner = SimBlockingRunner(threads)
        loop = asyncio.get_running_loop()

        def append_burst() -> FileRunLogWriter:
            writer = FileRunLogWriter(tmp_path / "run.log", clock=clock, loop=loop, threads=threads)
            for record in initial_records():
                writer.append(record)
            for seq in range(3, 303):
                writer.append(event(seq))
            return writer

        writer = await runner.run(append_burst)
        assert head(tmp_path) == 2
        await clock.sleep(0.11)
        assert head(tmp_path) == 302
        assert (tmp_path / "keyframes/302.pb").is_file()
        await runner.run(writer.close)

    run_virtual(clock, exercise())


def test_checkpoint_failure_keeps_old_head_until_recovery(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    writer = FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads())
    expected = initial_records()
    for record in expected:
        writer.append(record)
    for seq in range(3, 303):
        record = event(seq)
        expected.append(record)
        writer.append(record)
    old_head = (tmp_path / "run.head").read_bytes()
    checkpoint = tmp_path / "keyframes/302.pb"
    checkpoint.mkdir()
    with pytest.raises(OSError, match=r"302.pb"):
        writer.commit()
    assert (tmp_path / "run.head").read_bytes() == old_head
    checkpoint.rmdir()
    with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()) as recovered:
        assert recovered.sequence == 302
        assert head(tmp_path) == 302
        assert Keyframe.from_binary(checkpoint.read_bytes()) == fold_skeleton(
            expected, log_offset=path.stat().st_size
        )


def test_close_checkpoints_an_already_committed_epoch(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    writer = FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads())
    expected = [*initial_records(), event(3)]
    for record in expected:
        writer.append(record)
    writer.commit()
    assert not (tmp_path / "keyframes/3.pb").exists()
    writer.close()
    assert Keyframe.from_binary((tmp_path / "keyframes/3.pb").read_bytes()) == fold_skeleton(
        expected, log_offset=path.stat().st_size
    )
