"""Conversion is atomic and idempotent over a real journal and temporary home."""

from __future__ import annotations

import gzip
import json
import runpy
from contextlib import closing
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from tests.support.wire_goldens import REGENERATE

from vibesys.api.transport import ConversionStatus, convert_run, convert_runs
from vibesys.events import (
    AgentExecutionActivityData,
    AgentExecutionStartedData,
    AgentOutputChunkData,
    CoreEventType,
    GateFinishedData,
    GateKind,
    ObjectiveOverview,
    RunOverview,
    RunStartedData,
    SpanStartedData,
)
from vibesys.records.api import MappingState, map_event
from vibesys.run import EventJournal
from vibesys.run.runlog_tee import RunLogTee
from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    FlowState,
    RunEnvelope,
    Span,
    SpanLabel,
    SpanStatus,
    SpanType,
)
from vs_core.api.testing import FAKE_REGISTRY
from vs_project.api import Project, StoredEnvelope
from vs_proto.api import (
    API_SCHEMA_HASH,
    RUN_LOG_MAGIC,
    STORE_SCHEMA_HASH,
    AgentTurnAttrs,
    EpochStarted,
    Keyframe,
    LegacyEvent,
    MeasuredInput,
    Oneof,
    Record,
    RunHead,
)
from vs_runlog.api import (
    FileRunContent,
    FileRunLogWriter,
    RunLock,
    RunLogReader,
    encode_frame,
    fold_skeleton,
)
from vs_sim.api.testing import ManualClock, SimThreads


def journal_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock_start: float = 100
) -> tuple[Project, EventJournal]:
    """Build a JSONL-only run, as an older checkout would have written."""
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    project = Project.open(root)
    journal = EventJournal(ManualClock(start=clock_start), SimThreads())
    journal.attach(Project.log_directory_for(root, "run-1"), "run-1")
    return project, journal


def record_trace(journal: EventJournal) -> None:
    """Record a real flow span rather than a placeholder trace event."""
    journal.emit(
        CoreEventType.SPAN_STARTED,
        data=SpanStartedData(
            span=Span(
                id="flow:run#1",
                parent=None,
                type=SpanType.FLOW,
                name="run",
                status=SpanStatus.RUNNING,
                started_at=0.0,
            )
        ),
    )


def test_conversion_cannot_replace_a_live_writers_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    source = Project.event_journal_path_for(project.root, "run-1")
    journal.emit(
        CoreEventType.AGENT_OUTPUT_CHUNK,
        data=AgentOutputChunkData(channel="assistant", content="recorded output"),
    )
    original = source.read_bytes()
    destination = Project.run_log_path_for(project.root, "run-1")
    with closing(RunLock(destination.parent / "run.lock")), pytest.raises(BlockingIOError):
        convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    assert not destination.exists()
    assert source.read_bytes() == original
    assert not (destination.parent / "streams").exists()


def test_conversion_brings_the_runs_chat_conversations_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    journal.emit(
        CoreEventType.RUN_STARTED,
        "start",
        data=RunStartedData(outer_loop="single-agent", input="input"),
    )
    record_trace(journal)
    journal.emit(CoreEventType.RUN_FINISHED, "finish")
    directory = Project.log_directory_for(project.root, "run-1")
    chat = {"timestamp": "2026-01-01T00:00:01+00:00", "type": "chat", "data": {"kind": "chat"}}
    lines = [
        {
            "sequence": 1,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "type": "chat_thread_created",
            "chat_thread_id": "a1",
            "data": {
                "kind": "chat_thread_created",
                "thread_id": "a1",
                "title": "Why",
                "provider": "claude",
                "model": "opus",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        },
        {
            **chat,
            "sequence": 2,
            "text": "why?",
            "chat_thread_id": "a1",
            "data": {"kind": "chat", "answer": "because"},
        },
        {**chat, "sequence": 3, "text": "and?", "data": {"kind": "chat", "answer": "nothing"}},
    ]
    (directory / "run-events.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n"
    )

    first = convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    assert first.status is ConversionStatus.CONVERTED
    assert "imported 2 chat threads" in first.reason
    for thread, messages in (("a1", 2), ("default", 2)):
        log = Project.chat_session_directory_for(project.root, "run-1", thread) / "session.log"
        with log.open("rb") as stream:
            assert len(list(RunLogReader(stream))) == 1 + messages
    again = convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    assert again.status is ConversionStatus.EXISTS
    assert "imported" not in again.reason


@pytest.mark.parametrize("clock_start", [-0.5, 0.0, 100.25])
def test_conversion_preserves_source_and_timestamps_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock_start: float
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch, clock_start)
    journal.emit(
        CoreEventType.RUN_STARTED,
        "start",
        data=RunStartedData(outer_loop="single-agent", input="input"),
    )
    record_trace(journal)
    journal.emit(CoreEventType.RUN_FINISHED, "finish")
    source = Project.event_journal_path_for(project.root, "run-1")
    original = source.read_bytes()
    assert (
        convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads()).status
        is ConversionStatus.CONVERTED
    )
    destination = Project.run_log_path_for(project.root, "run-1")
    converted = destination.read_bytes()
    head = RunHead.from_binary((destination.parent / "run.head").read_bytes())
    keyframe = Keyframe.from_binary(
        (destination.parent / "keyframes" / f"{head.committed_seq}.pb").read_bytes()
    )
    assert keyframe.through_seq == head.committed_seq
    assert keyframe.log_offset == len(converted)
    with destination.open("rb") as stream:
        records = list(RunLogReader(stream))
    assert [record.seq for record in records] == [1, 2, 3, 4, 5]
    assert records[0].body is not None
    assert records[0].body.field == "epoch_started"
    assert all(
        record.at is not None and record.at.to_datetime() == journal.read()[0].timestamp
        for record in records
    )
    assert [record.body.field for record in records if record.body is not None] == [
        "epoch_started",
        "lifecycle",
        "span",
        "span",
        "lifecycle",
    ]
    assert (
        convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads()).status
        is ConversionStatus.EXISTS
    )
    assert destination.read_bytes() == converted
    assert source.read_bytes() == original
    assert sorted(path.name for path in destination.parent.iterdir()) == [
        "blobs",
        "core-events.jsonl",
        "keyframes",
        "run.head",
        "run.lock",
        "run.log",
    ]


def test_empty_journal_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, _ = journal_at(tmp_path, monkeypatch)
    Project.event_journal_path_for(project.root, "run-1").touch()
    result = convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    assert result.status is ConversionStatus.SKIPPED
    assert "no events" in result.reason
    assert not Project.run_log_path_for(project.root, "run-1").exists()


@given(chunks=st.integers(0, 4), ending=st.sampled_from(["run_finished", "run_failed"]))
def test_a_journal_that_predates_the_run_trace_converts_with_the_events_it_has(
    chunks: int, ending: str
) -> None:
    """The earliest journals have no span events; their run.log carries what they recorded."""
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as patch:
        project, journal = journal_at(Path(temporary), patch)
        journal.emit(
            CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="single-agent", input="in")
        )
        for _ in range(chunks):
            journal.emit(
                CoreEventType.AGENT_OUTPUT_CHUNK,
                data=AgentOutputChunkData(channel="assistant", content="output"),
            )
        journal.emit(CoreEventType(ending))
        result = convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
        assert result.status is ConversionStatus.CONVERTED
        with Project.run_log_path_for(project.root, "run-1").open("rb") as stream:
            bodies = [r.body.field for r in RunLogReader(stream) if r.body is not None]
        assert bodies[0] == "epoch_started"
        assert "span" not in bodies
        assert bodies[-1] == "lifecycle"
        assert (
            convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads()).status
            is ConversionStatus.EXISTS
        )


@given(
    input_value=st.floats(1, 100),
    candidates=st.lists(st.floats(1, 100), min_size=1, max_size=3),
)
def test_a_candidate_evaluated_before_any_workstream_span_is_not_a_second_input(
    input_value: float, candidates: list[float]
) -> None:
    """The oldest multi-agent journals record a workstream only when its round ends.

    The implementer's candidate is evaluated during the round, with spanless gate events. Its
    reading differs from the input's, and the run converts: the input's first measurement stays
    the run's input and the candidate's reading is an ordinary one.
    """
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as patch:
        project, journal = journal_at(Path(temporary), patch)
        overview = RunOverview(objective=ObjectiveOverview(text="t", metric="speed"))
        journal.emit(
            CoreEventType.RUN_STARTED,
            data=RunStartedData(outer_loop="multi-agent", input="in", overview=overview),
        )
        journal.emit(
            CoreEventType.GATE_FINISHED,
            data=GateFinishedData(gate=GateKind.BENCHMARK, metric="speed", value=input_value),
        )
        journal.emit(
            CoreEventType.GATE_FINISHED,
            execution_id="run-execution",
            data=GateFinishedData(gate=GateKind.BENCHMARK, metric="speed", value=input_value),
        )
        journal.emit(
            CoreEventType.AGENT_EXECUTION_STARTED,
            execution_id="turn-1",
            data=AgentExecutionStartedData(
                stage="multi-implementer",
                activity=AgentExecutionActivityData(mode="thinking", summary="working"),
            ),
        )
        for index, value in enumerate(candidates):
            journal.emit(
                CoreEventType.GATE_FINISHED,
                execution_id=f"vs-{index}",
                data=GateFinishedData(gate=GateKind.BENCHMARK, metric="speed", value=value),
            )
        result = convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
        assert result.status is ConversionStatus.CONVERTED, result.reason
        with Project.run_log_path_for(project.root, "run-1").open("rb") as stream:
            heads = [
                r.body.value
                for r in RunLogReader(stream)
                if r.body is not None and isinstance(r.body.value, EpochStarted)
            ]
        measurement = heads[-1].run.input_measurement if heads[-1].run is not None else None
        assert measurement is not None
        assert measurement.state is not None
        assert isinstance(measurement.state.value, MeasuredInput)
        assert measurement.state.value.value == input_value


@given(
    roles=st.lists(
        st.sampled_from(["orchestrator", "implementer", "judge", "profiler"]),
        min_size=1,
        max_size=5,
    ),
    model=st.sampled_from([None, "model-a"]),
)
def test_a_turn_span_without_an_agent_identity_converts_under_its_role(
    roles: list[str], model: str | None
) -> None:
    """The first journals labelled a turn with role and model only; its role is its agent."""
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as patch:
        project, journal = journal_at(Path(temporary), patch)
        journal.emit(
            CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="single-agent", input="in")
        )
        for index, role in enumerate(roles):
            label = SpanLabel(
                kind="agent_turn", title=role, attrs_json=json.dumps({"role": role, "model": model})
            )
            journal.emit(
                CoreEventType.SPAN_STARTED,
                data=SpanStartedData(
                    span=Span(
                        id=f"op:{index}/{role}.1",
                        parent=None,
                        type=SpanType.OPERATION,
                        name=role,
                        status=SpanStatus.RUNNING,
                        started_at=float(index),
                        label=label,
                    )
                ),
            )
        result = convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
        assert result.status is ConversionStatus.CONVERTED
        turns = []
        with Project.run_log_path_for(project.root, "run-1").open("rb") as stream:
            for record in RunLogReader(stream):
                if record.body is None or record.body.field != "span":
                    continue
                span = record.body.value.span
                assert span is not None
                assert span.label is not None
                assert span.label.attrs is not None
                turns.append(AgentTurnAttrs.from_binary(span.label.attrs.value))
        assert [(turn.agent, turn.role, turn.model or None) for turn in turns] == [
            (role, role, model) for role in roles
        ]


def test_conversion_replaces_torn_destination_only_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    destination = Project.run_log_path_for(project.root, "run-1")
    torn = RUN_LOG_MAGIC + b"\x80"
    destination.write_bytes(torn)
    source = Project.event_journal_path_for(project.root, "run-1")
    original = source.read_bytes()
    with source.open("ab") as stream:
        stream.write(b"{malformed}")
    with pytest.raises(ValueError, match="journal event"):
        convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    assert destination.read_bytes() == torn
    source.write_bytes(original)
    assert (
        convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads()).status
        is ConversionStatus.CONVERTED
    )


def test_corrupt_destination_is_reported_without_overwriting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    destination = Project.run_log_path_for(project.root, "run-1")
    destination.write_bytes(b"BADMAGIC")
    with pytest.raises(ValueError, match="byte 0"):
        convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    assert destination.read_bytes() == b"BADMAGIC"


def test_conversion_upgrades_legacy_records_and_externalizes_large_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    content = "\u2603" * 400000
    journal.emit(
        CoreEventType.AGENT_OUTPUT_CHUNK,
        data=AgentOutputChunkData(channel="assistant", content=content),
        execution_id="turn-a",
    )
    destination = Project.run_log_path_for(project.root, "run-1")
    with FileRunLogWriter(destination, clock=ManualClock(), threads=SimThreads()) as writer:
        writer.append(Record(seq=1, epoch=1, body=Oneof("epoch_started", EpochStarted())))
        writer.append(Record(seq=2, epoch=1, body=Oneof("legacy_event", LegacyEvent(json="old"))))
    original = Project.event_journal_path_for(project.root, "run-1").read_bytes()
    assert (
        convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads()).status
        is ConversionStatus.CONVERTED
    )
    with destination.open("rb") as stream:
        records = list(RunLogReader(stream))
    assert all(
        record.body is not None and record.body.field != "legacy_event" for record in records
    )
    attachment = next(
        record.body.value
        for record in records
        if record.body is not None and record.body.field == "blob"
    )
    assert attachment.blob is not None
    path = Project.run_blobs_directory_for(project.root, "run-1") / attachment.blob.sha256
    assert path.read_bytes() == content.encode()
    assert Project.event_journal_path_for(project.root, "run-1").read_bytes() == original
    assert (
        convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads()).status
        is ConversionStatus.EXISTS
    )


@pytest.mark.parametrize("empty_header", [False, True])
def test_conversion_repairs_a_tee_with_no_event_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, empty_header: bool
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    destination = Project.run_log_path_for(project.root, "run-1")
    with FileRunLogWriter(destination, clock=ManualClock(), threads=SimThreads()) as writer:
        if empty_header:
            writer.append(Record(seq=1, epoch=1, body=Oneof("epoch_started", EpochStarted())))
    assert (
        convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads()).status
        is ConversionStatus.CONVERTED
    )
    with destination.open("rb") as stream:
        records = list(RunLogReader(stream))
    assert any(record.body is not None and record.body.field == "span" for record in records)


@pytest.mark.regression
@given(
    names=st.lists(st.text(alphabet="abcXYZ012", min_size=1, max_size=12), min_size=1, max_size=30)
)
@example(names=["last-span"])
def test_conversion_fold_and_every_keyframe_suffix_equal_the_typed_mapping(
    names: list[str],
) -> None:
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as patch:
        project, journal = journal_at(Path(temporary), patch)
        journal.emit(
            CoreEventType.RUN_STARTED,
            data=RunStartedData(outer_loop="single-agent", input="input"),
        )
        for i, name in enumerate(names):
            journal.emit(
                CoreEventType.SPAN_STARTED,
                data=SpanStartedData(
                    span=Span(
                        id=f"flow:{i}",
                        name=name,
                        parent=None,
                        type=SpanType.FLOW,
                        status=SpanStatus.RUNNING,
                        started_at=float(i),
                    )
                ),
            )
        source = Project.event_journal_path_for(project.root, "run-1")
        before = source.read_bytes()
        assert (
            convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads()).status
            is ConversionStatus.CONVERTED
        )
        destination = Project.run_log_path_for(project.root, "run-1")
        with destination.open("rb") as stream:
            records = list(RunLogReader(stream))
        state = MappingState(
            "run-1",
            vibesys_version=version("vibesys"),
            wall_span_clock=True,
            clock_epochs=((journal.read()[0].timestamp.timestamp(), 0),),
        )
        expected = []
        for event in journal.read():
            mapped = map_event(state, event)
            expected.extend(mapped.records)
            state = mapped.state
        size = destination.stat().st_size
        full = fold_skeleton(records, log_offset=size)
        assert full == fold_skeleton(expected, log_offset=size)
        checkpoints = sorted((destination.parent / "keyframes").glob("*.pb"))
        assert checkpoints
        assert (destination.parent / "keyframes" / f"{records[-1].seq}.pb").exists()
        for path in checkpoints:
            seed = Keyframe.from_binary(path.read_bytes())
            with destination.open("rb") as stream:
                suffix = list(RunLogReader(stream, offset=seed.log_offset))
            assert fold_skeleton(suffix, seed=seed, log_offset=size) == full
        files = {
            p.relative_to(destination.parent): p.read_bytes()
            for p in destination.parent.rglob("*")
            if p.is_file()
        }
        assert (
            convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads()).status
            is ConversionStatus.EXISTS
        )
        assert {
            p.relative_to(destination.parent): p.read_bytes()
            for p in destination.parent.rglob("*")
            if p.is_file()
        } == files
        assert source.read_bytes() == before


@pytest.mark.parametrize("missing", ["head", "keyframes", "both"])
def test_conversion_repairs_old_complete_log_without_rewriting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    journal.emit(
        CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="single-agent", input="input")
    )
    record_trace(journal)
    journal.emit(CoreEventType.RUN_FINISHED)
    convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    directory = Project.log_directory_for(project.root, "run-1")
    original = (directory / "run.log").read_bytes()
    if missing in {"head", "both"}:
        (directory / "run.head").unlink()
    if missing in {"keyframes", "both"}:
        for path in (directory / "keyframes").iterdir():
            path.unlink()
    result = convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    assert result.status is ConversionStatus.CONVERTED
    assert "repaired" in result.reason
    head = RunHead.from_binary((directory / "run.head").read_bytes())
    seed = Keyframe.from_binary((directory / "keyframes" / f"{head.committed_seq}.pb").read_bytes())
    assert seed.log_offset == len(original)
    assert (directory / "run.log").read_bytes() == original
    assert (
        convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads()).status
        is ConversionStatus.EXISTS
    )


@pytest.mark.parametrize(
    "path",
    sorted((Path(__file__).parents[3] / "libs/vs-core/tests/envelopes").glob("v*-*.json")),
    ids=lambda p: p.stem,
)
def test_phase_two_conversion_preserves_saved_core_envelopes_and_their_resume_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: Path
) -> None:
    """Golden saves remain resumable; phase 2 never invents a state.pb commit fence."""
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    envelope = path.read_text()
    # The store owns an opaque runtime payload, not the core schema version.
    payload = json.dumps({"schema_version": 1, "envelope": json.loads(envelope)})
    record = StoredEnvelope(revision=4, schema_version=1, payload=payload.encode())
    original = json.dumps({"version": 1, "record": record.model_dump(mode="json")}).encode()
    namespace = project.state.state_store_namespace("run-1")
    namespace.write_bytes("store.json", original)
    result = convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    assert result.status is ConversionStatus.CONVERTED
    assert "state.pb conversion is phase 3" in result.reason
    assert namespace.read_bytes("store.json") == original
    assert project.stored_record("run-1") == record
    decoded = FAKE_REGISTRY.decode_envelope(RunEnvelope[FlowState], envelope)
    assert decoded.schema_version == ENVELOPE_SCHEMA_VERSION
    assert not (Project.log_directory_for(project.root, "run-1") / "state.pb").exists()


def test_bulk_reports_every_project_and_index_outcome_and_continues_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    # Project source and its existing host index alias are both reported.
    Project.publish_run_for(project.root, "run-1")
    for run_id, content in {
        "bad": b"{malformed}",
        "empty": b"",
        "old": b'{"type":"retired_event","timestamp":"2026-01-01T00:00:00Z"}',
    }.items():
        directory = Project.log_directory_for(project.root, run_id)
        directory.mkdir(parents=True)
        (directory / "core-events.jsonl").write_bytes(content)
    pretrace = EventJournal(ManualClock(start=100), SimThreads())
    pretrace.attach(Project.log_directory_for(project.root, "pretrace"), "pretrace")
    pretrace.emit(CoreEventType.RUN_FINISHED)
    other = tmp_path / "other"
    other.mkdir()
    external = EventJournal(ManualClock(start=100), SimThreads())
    external.attach(Project.log_directory_for(other, "external"), "external")
    record_trace(external)
    external_link = Project.publish_run_for(other, "external")
    index = external_link.parent
    (index / "broken").symlink_to(tmp_path / "absent")
    (index / "unsupported").write_bytes(b"keep")
    rows = {
        row.run: row.result
        for row in convert_runs(project, clock=ManualClock(), threads=SimThreads())
    }
    assert rows["run-1"].status is ConversionStatus.CONVERTED
    assert rows["bad"].status is ConversionStatus.FAILED
    assert rows["empty"].status is ConversionStatus.SKIPPED
    assert rows["old"].status is ConversionStatus.SKIPPED
    assert "unsupported journal event" in rows["old"].reason
    assert rows["pretrace"].status is ConversionStatus.CONVERTED
    assert rows[str(external_link)].status is ConversionStatus.CONVERTED
    assert rows[str(index / "broken")].status is ConversionStatus.FAILED
    assert rows[str(index / "unsupported")].status is ConversionStatus.SKIPPED
    assert "already processed" in rows[str(index / "run-1")].reason
    assert len(rows) == 9
    second = {
        row.run: row.result
        for row in convert_runs(project, clock=ManualClock(), threads=SimThreads())
    }
    assert second["run-1"].status is ConversionStatus.SKIPPED
    assert second["run-1"].reason == "already converted"
    assert second[str(external_link)].reason == "already converted"
    assert (index / "unsupported").read_bytes() == b"keep"


def test_bulk_reports_a_store_only_run_as_unsupported_in_phase_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    project = Project.open(root)
    namespace = project.state.state_store_namespace("store-only")
    namespace.write_bytes("store.json", b"original core state")
    rows = convert_runs(project, clock=ManualClock(), threads=SimThreads())
    assert len(rows) == 1
    assert rows[0].run == "store-only"
    assert rows[0].result.status is ConversionStatus.SKIPPED
    assert "state.pb" in rows[0].result.reason
    assert namespace.read_bytes("store.json") == b"original core state"


def test_conversion_header_records_the_live_schema_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed goldens pin these hashes, so this is where the live ones are checked."""
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    with Project.run_log_path_for(project.root, "run-1").open("rb") as stream:
        header = next(iter(RunLogReader(stream))).body
    assert header is not None
    assert header.field == "epoch_started"
    assert header.value.api_schema_hash == API_SCHEMA_HASH
    assert header.value.store_schema_hash == STORE_SCHEMA_HASH


def test_gateway_conversion_fixture_regenerates_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = journal_at(tmp_path, monkeypatch)
    directory = Path(__file__).parents[3] / "proto/testdata/conversion"
    generated = runpy.run_path(str(directory / "generate.py"))["fixtures"](project)
    for name, payload in generated.items():
        saved = (directory / name).read_bytes()
        message = (
            f"proto/testdata/conversion/{name} differs; inspect the difference, then regenerate "
            f"with: {REGENERATE}"
        )
        if name.endswith(".gz"):
            assert gzip.decompress(saved) == gzip.decompress(payload), message
        else:
            assert saved == payload, message


@pytest.mark.parametrize("damage", ["log-sequence", "keyframe-offset", "stale-head"])
def test_conversion_reports_or_repairs_damaged_derived_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    journal.emit(
        CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="single-agent", input="input")
    )
    record_trace(journal)
    journal.emit(CoreEventType.RUN_FINISHED)
    convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    directory = Project.log_directory_for(project.root, "run-1")
    head = RunHead.from_binary((directory / "run.head").read_bytes())
    if damage == "log-sequence":
        with (directory / "run.log").open("rb") as stream:
            records = list(RunLogReader(stream))
        records[-1].seq += 1
        (directory / "run.log").write_bytes(
            RUN_LOG_MAGIC + b"".join(encode_frame(r) for r in records)
        )
        with pytest.raises(ValueError, match="non-dense"):
            convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    elif damage == "keyframe-offset":
        path = directory / "keyframes" / f"{head.committed_seq}.pb"
        seed = Keyframe.from_binary(path.read_bytes())
        seed.log_offset += 1
        path.write_bytes(seed.to_binary())
        with pytest.raises(ValueError, match="checkpoint does not match"):
            convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
    else:
        head.committed_seq -= 1
        (directory / "run.head").write_bytes(head.to_binary())
        assert (
            convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads()).status
            is ConversionStatus.CONVERTED
        )
        assert (
            convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads()).status
            is ConversionStatus.EXISTS
        )


def test_bulk_reports_index_publication_failure_and_preserves_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    home = tmp_path / "home"
    (home / "runs").mkdir()
    (home / "runs/run-1").write_bytes(b"keep")
    results = convert_runs(project, clock=ManualClock(), threads=SimThreads())
    assert results[0].result.status is ConversionStatus.FAILED
    assert "not a symlink" in results[0].result.reason
    assert results[1].result.status is ConversionStatus.SKIPPED
    assert (home / "runs/run-1").read_bytes() == b"keep"


def test_typed_tee_explicit_commit_publishes_pending_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    for _ in range(300):
        journal.emit(CoreEventType.FRAMEWORK_WARNING, text="progress")
    destination = Project.run_log_path_for(project.root, "run-1")
    writer = FileRunLogWriter(destination, clock=ManualClock(), threads=SimThreads())
    content = FileRunContent(
        Project.run_blobs_directory_for(project.root, "run-1"),
        Project.log_streams_directory_for(project.root, "run-1"),
    )
    with RunLogTee(writer, content, "run-1") as tee:
        source = Project.event_journal_path_for(project.root, "run-1")
        for line in source.read_bytes().splitlines():
            tee.append(line)
        with destination.open("rb") as stream:
            records = list(RunLogReader(stream))
        checkpoint = destination.parent / "keyframes" / f"{records[-1].seq}.pb"
        assert not checkpoint.exists()
        tee.commit()
        assert Keyframe.from_binary(checkpoint.read_bytes()).through_seq == records[-1].seq


def usage_journal(rows: list[tuple[int, int]]) -> list[bytes]:
    """A retained journal of one turn whose usage lines are (source sequence, input tokens)."""
    at = "2026-01-01T00:00:00Z"
    span = {
        "id": "op:turn-a",
        "parent": None,
        "type": "operation",
        "name": "ask",
        "status": "running",
        "started_at": 0,
        "label": {
            "kind": "agent_turn",
            "attrs_json": json.dumps({"agent": "turn-a", "role": "implementer"}),
        },
    }
    head = [
        {
            "sequence": 1,
            "type": "run_started",
            "data": {"kind": "run_started", "outer_loop": "test", "input": "input"},
        },
        {"sequence": 2, "type": "span_started", "data": {"kind": "span_started", "span": span}},
    ]
    usage = [
        {
            "sequence": sequence,
            "type": "turn_usage_recorded",
            "data": {"kind": "turn_usage_recorded", "span_id": "op:turn-a", "input_tokens": tokens},
        }
        for sequence, tokens in rows
    ]
    end = {"sequence": 1000, "type": "run_finished", "data": None}
    return [
        json.dumps({"run_id": "run-1", "timestamp": at, **row}).encode() + b"\n"
        for row in [*head, *usage, end]
    ]


def converted_input_tokens(project: Project) -> int | None:
    latest: dict[str, AgentTurnAttrs] = {}
    with Project.run_log_path_for(project.root, "run-1").open("rb") as stream:
        for record in RunLogReader(stream):
            if record.body is None or record.body.field != "span":
                continue
            span = record.body.value.span
            if span is not None and span.label is not None and span.label.attrs is not None:
                latest[span.id] = AgentTurnAttrs.from_binary(span.label.attrs.value)
    amounts = [attrs.usage.input for attrs in latest.values() if attrs.usage is not None]
    return sum(amounts) if amounts else None


@given(
    sources=st.dictionaries(st.integers(3, 900), st.integers(1, 10_000), min_size=1, max_size=5),
    deliveries=st.lists(st.integers(0, 4), min_size=1, max_size=12),
)
@example(sources={3: 100}, deliveries=[0, 0])
def test_conversion_counts_a_repeated_source_usage_line_once(
    sources: dict[int, int], deliveries: list[int]
) -> None:
    """Whatever lines repeat or arrive late, each source sequence adds its tokens once."""
    order = list(sources.items())
    rows = [order[index % len(order)] for index in deliveries] + order
    expected = sum(sources.values())
    with TemporaryDirectory() as directory:
        task = Path(directory)
        root = task / "project"
        root.mkdir()
        with pytest.MonkeyPatch.context() as env:
            env.setenv("VIBESYS_STATE_HOME", str(task / "home"))
            project = Project.open(root)
            journal = Project.event_journal_path_for(root, "run-1")
            journal.parent.mkdir(parents=True)
            journal.write_bytes(b"".join(usage_journal(rows)))
            convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
            assert converted_input_tokens(project) == expected


@given(
    sources=st.dictionaries(
        st.integers(3, 900),
        st.tuples(st.integers(1, 10_000), st.booleans()),
        min_size=1,
        max_size=5,
    ),
    deliveries=st.lists(st.integers(0, 4), min_size=1, max_size=12),
    binding_at=st.integers(0, 12),
)
@example(sources={3: (100, False)}, deliveries=[0, 0], binding_at=1)
def test_conversion_usage_identity_survives_late_span_bindings(
    sources: dict[int, tuple[int, bool]], deliveries: list[int], binding_at: int
) -> None:
    """Source identity survives duplicates before/after binding, optional spans and reordered usage."""
    order = list(sources)
    rows = [order[index % len(order)] for index in deliveries] + order
    documents = [
        json.loads(line)
        for line in usage_journal([(sequence, sources[sequence][0]) for sequence in rows])
    ]
    binding = documents.pop(1)
    binding["data"]["span"]["invocation"] = "turn-a"
    for document in documents[1:-1]:
        document["execution_id"] = "turn-a"
        if not sources[document["sequence"]][1]:
            document["data"].pop("span_id")
    documents.insert(1 + binding_at % len(rows), binding)
    with TemporaryDirectory() as directory:
        task = Path(directory)
        root = task / "project"
        root.mkdir()
        with pytest.MonkeyPatch.context() as env:
            env.setenv("VIBESYS_STATE_HOME", str(task / "home"))
            project = Project.open(root)
            source = Project.event_journal_path_for(root, "run-1")
            source.parent.mkdir(parents=True)
            source.write_bytes(
                b"".join(json.dumps(document).encode() + b"\n" for document in documents)
            )
            result = convert_run(project, "run-1", clock=ManualClock(), threads=SimThreads())
            assert result.status == ConversionStatus.CONVERTED
            assert converted_input_tokens(project) == sum(tokens for tokens, _ in sources.values())
