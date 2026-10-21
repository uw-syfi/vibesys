"""Epoch recovery through the live journal and actual project files."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_proto.api as p
from vibesys.events import CoreEventType, RunStartedData, make_core_event
from vibesys.run import EventJournal
from vibesys.run.runlog_tee import RunLogTee
from vs_project.api import Project
from vs_runlog.api import (
    FileRunContent,
    FileRunLogWriter,
    RunLogReader,
    assert_run_unlocked,
    encode_frame,
)
from vs_sim.api.testing import ManualClock, SimThreads


def read(path: Path) -> list[p.Record]:
    with path.open("rb") as stream:
        return list(RunLogReader(stream))


def test_relaunch_freezes_clock_and_records_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    project = Project.open(root)
    clock = ManualClock(start=100)
    journal = EventJournal(clock, SimThreads())
    journal.checkout = ("tree-a", True)
    journal.enable_runlog(project, "run-a")
    journal.attach(Project.log_directory_for(root, "run-a"), "run-a")
    journal.emit(CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="test", input="input"))
    clock.advance(5)
    journal.emit(CoreEventType.RESUMED)
    journal.close()
    clock.advance(1000)
    journal = EventJournal(clock, SimThreads())
    journal.checkout = ("tree-b", False)
    journal.enable_runlog(project, "run-a")
    journal.attach(Project.log_directory_for(root, "run-a"), "run-a")
    journal.emit(CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="test", input="input"))
    clock.advance(3)
    journal.emit(CoreEventType.RUN_FINISHED)
    journal.close()
    records = read(Project.run_log_path_for(root, "run-a"))
    assert [r.clock for r in records] == [0, 5, 5, 5, 8]
    assert [r.at.to_datetime().timestamp() for r in records] == [100, 105, 1105, 1105, 1108]
    headers = [r.body.value for r in records if r.body and r.body.field == "epoch_started"]
    assert [(h.epoch, h.tree_hash, h.dirty, h.provenance_known) for h in headers] == [
        (1, "tree-a", True, True),
        (2, "tree-b", False, True),
    ]
    ends = [
        r
        for r in records
        if r.body and r.body.field == "lifecycle" and r.body.value.state.field == "ended"
    ]
    assert [(r.epoch, r.body.value.state.value.outcome) for r in ends] == [
        (1, p.Outcome.INTERRUPTED),
        (2, p.Outcome.FINISHED),
    ]
    for record in ends:
        assert (
            Project.run_log_path_for(root, "run-a").parent / "keyframes" / f"{record.seq}.pb"
        ).exists()


@given(cut=st.integers(min_value=0, max_value=400))
def test_crash_at_every_epoch_write_prefix_has_one_end(cut: int) -> None:
    with TemporaryDirectory() as directory:
        assert_crash_prefix(Path(directory), cut)


def assert_crash_prefix(tmp_path: Path, cut: int) -> None:
    # Prefixes include a crash between Ended and EpochStarted and torn frames.
    at = datetime(2026, 1, 1, tzinfo=UTC)
    first = p.Record(
        seq=1,
        epoch=1,
        at=p.Timestamp.from_datetime(at),
        clock=7,
        body=p.Oneof("epoch_started", p.EpochStarted(epoch=1, run_id="run-a")),
    )
    ended = p.Record(
        seq=2,
        epoch=1,
        at=p.Timestamp.from_datetime(at),
        clock=7,
        body=p.Oneof(
            "lifecycle", p.Lifecycle(state=p.Oneof("ended", p.Ended(outcome=p.Outcome.INTERRUPTED)))
        ),
    )
    second = p.Record(
        seq=3,
        epoch=2,
        at=p.Timestamp.from_datetime(at),
        clock=7,
        body=p.Oneof("epoch_started", p.EpochStarted(epoch=2, run_id="run-a")),
    )
    path = tmp_path / "run.log"
    suffix = encode_frame(ended) + encode_frame(second)
    path.write_bytes(p.RUN_LOG_MAGIC + encode_frame(first) + suffix[:cut])
    with RunLogTee(
        FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()),
        FileRunContent(tmp_path / "blobs", tmp_path / "streams"),
        "run-a",
    ) as tee:
        tee.append(
            make_core_event(CoreEventType.RESUMED, timestamp=at + timedelta(days=1))
            .model_dump_json()
            .encode()
        )
    records = read(path)
    final_epoch = records[-1].epoch
    for epoch in range(1, final_epoch):
        ends = [
            r
            for r in records
            if r.epoch == epoch
            and r.body
            and r.body.field == "lifecycle"
            and r.body.value.state.field == "ended"
        ]
        assert len(ends) == 1
    assert all(r.clock == 7 for r in records)


def test_live_writer_refuses_relaunch_without_mutating_log(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()):
        before = path.read_bytes()
        with pytest.raises(RuntimeError, match=r"run\.lock is held"):
            assert_run_unlocked(path)
        assert path.read_bytes() == before
    assert_run_unlocked(path)
