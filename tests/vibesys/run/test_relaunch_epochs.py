"""Epoch recovery through the live journal and actual project files."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter

import vs_proto.api as p
from vibesys.events import (
    CoreEventData,
    CoreEventType,
    RunFailedData,
    RunFailure,
    RunFailureKind,
    RunStartedData,
    SpanEndedData,
    SpanStartedData,
    make_core_event,
)
from vibesys.run import EventJournal
from vibesys.run.runlog_tee import RunLogTee
from vs_core.api import SpanStatus, WireSpan
from vs_project.api import Project
from vs_runlog.api import (
    FileRunContent,
    FileRunLogWriter,
    RunLogReader,
    assert_run_unlocked,
    encode_frame,
)
from vs_sim.api.testing import ManualClock, SimThreads, examples

_SYNTHETIC = "previous writer exited without ending its epoch"
"""The interrupt a successor writes when the epoch it replaces never ended."""


def _failure(kind: RunFailureKind) -> RunFailedData:
    return RunFailedData(
        failure=RunFailure(
            kind=kind,
            reason=kind.value,
            workstreams_started=0,
            workstream_budget=1,
            candidates_kept=0,
        )
    )


@dataclass(frozen=True)
class _Close:
    """How the first epoch stops, and whether that stop is an ending."""

    name: str
    ended: bool
    events: tuple[tuple[CoreEventType, CoreEventData | None], ...]


def _closes() -> tuple[_Close, ...]:
    failures = tuple(
        _Close(
            kind.value,
            ended=True,
            events=((CoreEventType.RUN_FAILED, _failure(kind)),),
        )
        for kind in RunFailureKind
    )
    return (
        _Close("parked", ended=True, events=((CoreEventType.PARKED, None),)),
        _Close("finished", ended=True, events=((CoreEventType.RUN_FINISHED, None),)),
        _Close("stopped", ended=True, events=((CoreEventType.STOPPED, None),)),
        *failures,
        _Close("running", ended=False, events=()),
        _Close("draining", ended=False, events=((CoreEventType.PARK_REQUESTED, None),)),
    )


_CLOSES = {close.name: close for close in _closes()}


def _turn(
    root: Path, closing: tuple[tuple[CoreEventType, CoreEventData | None], ...]
) -> list[p.Record]:
    """Park or end ``closing``, then Resume and record one turn on the successor."""
    project = Project.open(root)
    clock = ManualClock(start=100)

    def open_journal() -> EventJournal:
        journal = EventJournal(clock, SimThreads())
        journal.enable_runlog(project, "run-a")
        journal.attach(Project.log_directory_for(root, "run-a"), "run-a")
        return journal

    journal = open_journal()
    journal.emit(CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="test", input="input"))
    for event_type, data in closing:
        journal.emit(event_type, data=data)
    journal.close()
    clock.advance(1000)
    journal = open_journal()
    span = TypeAdapter(WireSpan).validate_python(
        {
            "id": "op:implementer.1",
            "parent": None,
            "type": "operation",
            "name": "implementer",
            "status": "running",
            "started_at": 200,
        }
    )
    journal.emit(CoreEventType.SPAN_STARTED, data=SpanStartedData(span=span))
    journal.close()
    return read(Project.run_log_path_for(root, "run-a"))


def _synthetic(records: list[p.Record]) -> list[p.Record]:
    found = []
    for record in records:
        body = record.body
        if (
            body is None
            or body.field != "lifecycle"
            or body.value.state is None
            or body.value.state.field != "ended"
        ):
            continue
        ending = body.value.state.value
        if ending.interruption_reason == _SYNTHETIC:
            found.append(record)
    return found


@pytest.mark.regression
@example(name="parked")
@example(name="deadline")
@settings(max_examples=examples(pr=len(_CLOSES), nightly=len(_CLOSES)), deadline=None)
@given(name=st.sampled_from(sorted(_CLOSES)))
def test_resume_after_an_ending_opens_a_fresh_epoch_that_records_a_turn(name: str) -> None:
    """Resume never invents an interrupt for an epoch that already ended.

    A parked epoch and every ended outcome (finished, failed, including a deadline,
    stopped) are endings. Starting, running and draining are not: a writer that
    exits then still leaves one interrupt, and the successor still records its turn.
    """
    close = _CLOSES[name]
    with TemporaryDirectory() as directory:
        records = _turn(Path(directory), close.events)
    synthetic = _synthetic(records)
    spans = [
        record
        for record in records
        if record.body is not None and record.body.field == "span" and record.body.value.span
    ]
    assert spans
    assert spans[-1].epoch == 2
    assert spans[-1].body is not None
    assert spans[-1].body.value.span is not None
    assert spans[-1].body.value.span.name == "implementer"
    if close.ended:
        assert synthetic == []
        return
    assert len(synthetic) == 1
    assert synthetic[0].epoch == 1


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
    assert [r.clock for r in records] == [0, 0, 0, 5, 5, 5, 5, 5, 8]
    assert [r.at.to_datetime().timestamp() for r in records if r.at is not None] == [
        100,
        100,
        100,
        105,
        1105,
        1105,
        1105,
        1105,
        1108,
    ]
    headers = [r.body.value for r in records if r.body and r.body.field == "epoch_started"]
    assert [(h.epoch, h.tree_hash, h.dirty, h.provenance_known) for h in headers] == [
        (1, "tree-a", True, True),
        (1, "tree-a", True, True),
        (2, "tree-b", False, True),
        (2, "tree-b", False, True),
    ]
    ends = [
        (r, r.body.value.state.value.outcome)
        for r in records
        if r.body
        and r.body.field == "lifecycle"
        and r.body.value.state is not None
        and r.body.value.state.field == "ended"
    ]
    assert [(r.epoch, outcome) for r, outcome in ends] == [
        (1, p.Outcome.INTERRUPTED),
        (2, p.Outcome.FINISHED),
    ]
    for record, _ in ends:
        assert (
            Project.run_log_path_for(root, "run-a").parent / "keyframes" / f"{record.seq}.pb"
        ).exists()


@given(cut=st.integers(min_value=0, max_value=400))
def test_crash_at_every_epoch_write_prefix_has_one_end(cut: int) -> None:
    with TemporaryDirectory() as directory:
        assert_crash_prefix(Path(directory), cut)


def assert_crash_prefix(tmp_path: Path, cut: int | None) -> None:
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
    if cut is None:
        cut = len(encode_frame(ended))
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
            and r.body.value.state is not None
            and r.body.value.state.field == "ended"
        ]
        assert len(ends) == 1
    assert all(r.clock == 7 for r in records)


def test_crash_between_the_two_epoch_writes_does_not_duplicate_the_end(tmp_path: Path) -> None:
    assert_crash_prefix(tmp_path, None)


@pytest.mark.parametrize("initial_clock", [None, 7.0])
def test_relaunch_recovers_epoch_clock_origins(tmp_path: Path, initial_clock: float | None) -> None:
    path = tmp_path / "run.log"
    records = [
        p.Record(
            seq=1,
            epoch=1,
            clock=initial_clock,
            at=p.Timestamp(seconds=100),
            body=p.Oneof("epoch_started", p.EpochStarted(epoch=1, run_id="run-a")),
        ),
        p.Record(
            seq=2,
            epoch=1,
            clock=None if initial_clock is None else initial_clock + 5,
            at=p.Timestamp(seconds=105),
            body=p.Oneof("lifecycle", p.Lifecycle(state=p.Oneof("running", p.Running()))),
        ),
    ]
    path.write_bytes(p.RUN_LOG_MAGIC + b"".join(encode_frame(r) for r in records))
    with RunLogTee(
        FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()),
        FileRunContent(tmp_path / "blobs", tmp_path / "streams"),
        "run-a",
        checkout=("tree-a", False),
    ) as tee:
        tee.append(
            make_core_event(CoreEventType.RESUMED, timestamp=datetime.fromtimestamp(1105, tz=UTC))
            .model_dump_json()
            .encode()
        )
        span = TypeAdapter(WireSpan).validate_python(
            {
                "id": "flow:old#1",
                "parent": None,
                "type": "flow",
                "name": "old",
                "status": "running",
                "started_at": 101,
            }
        )
        tee.append(
            make_core_event(
                CoreEventType.SPAN_STARTED,
                data=SpanStartedData(span=span),
                timestamp=datetime.fromtimestamp(1106, tz=UTC),
            )
            .model_dump_json()
            .encode()
        )
    resumed = read(path)
    base = initial_clock or 0
    assert [r.clock for r in resumed[2:]] == [base + 5, base + 5, base + 5, base + 6]
    body = resumed[-1].body
    assert body is not None
    assert body.field == "span"
    assert body.value.span is not None
    assert body.value.span.started_at == base + 1


def test_live_writer_refuses_relaunch_without_mutating_log(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()):
        before = path.read_bytes()
        with pytest.raises(RuntimeError, match=r"run\.lock is held"):
            assert_run_unlocked(path)
        assert path.read_bytes() == before
    assert_run_unlocked(path)


def test_span_times_keep_their_original_epoch_on_relaunch(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    clock = ManualClock(start=100)

    def open_journal() -> EventJournal:
        journal = EventJournal(clock, SimThreads())
        journal.enable_runlog(project, "run-spans")
        journal.attach(Project.log_directory_for(tmp_path, "run-spans"), "run-spans")
        journal.emit(
            CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="test", input="input")
        )
        return journal

    journal = open_journal()
    clock.advance(5)
    span = TypeAdapter(WireSpan).validate_python(
        {
            "id": "flow:work#1",
            "parent": None,
            "type": "flow",
            "name": "work",
            "status": "running",
            "started_at": 102,
        }
    )
    journal.emit(CoreEventType.SPAN_STARTED, data=SpanStartedData(span=span))
    journal.close()
    clock.advance(1000)
    journal = open_journal()
    clock.advance(3)
    finished = span.model_copy(update={"status": SpanStatus.DONE, "ended_at": 1108})
    journal.emit(CoreEventType.SPAN_ENDED, data=SpanEndedData(span=finished))
    journal.emit(CoreEventType.STOPPED)
    journal.emit(CoreEventType.RUN_FINISHED)
    journal.close()
    records = read(Project.run_log_path_for(tmp_path, "run-spans"))
    spans = [
        r.body.value.span
        for r in records
        if r.body and r.body.field == "span" and r.body.value.span is not None
    ]
    assert [(s.started_at, s.ended_at if s.has_field("ended_at") else None) for s in spans] == [
        (2, None),
        (2, 8),
    ]
    ends = [
        r.epoch
        for r in records
        if r.body
        and r.body.field == "lifecycle"
        and r.body.value.state is not None
        and r.body.value.state.field == "ended"
    ]
    assert ends == [1, 2]
