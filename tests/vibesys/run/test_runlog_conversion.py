"""Conversion is atomic and idempotent over a real journal and temporary home."""

from __future__ import annotations

import gzip
import json
import runpy
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vibesys.api.transport import ConversionStatus, convert_run, convert_runs
from vibesys.events import AgentOutputChunkData, CoreEventType, RunStartedData, SpanStartedData
from vibesys.records.api import MappingState, map_event
from vibesys.run import EventJournal
from vs_core.api import ENVELOPE_SCHEMA_VERSION, FlowState, RunEnvelope, Span, SpanStatus, SpanType
from vs_core.api.testing import FAKE_REGISTRY
from vs_project.api import Project, StoredEnvelope
from vs_proto.api import RUN_LOG_MAGIC, EpochStarted, Keyframe, LegacyEvent, Oneof, Record, RunHead
from vs_runlog.api import FileRunLogWriter, RunLogReader, encode_frame, fold_skeleton
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
    assert convert_run(project, "run-1").status is ConversionStatus.CONVERTED
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
    assert [record.seq for record in records] == [1, 2, 3]
    assert records[0].body is not None
    assert records[0].body.field == "epoch_started"
    assert all(
        record.at is not None and record.at.to_datetime() == journal.read()[0].timestamp
        for record in records
    )
    assert [record.body.field for record in records if record.body is not None] == [
        "epoch_started",
        "span",
        "lifecycle",
    ]
    assert convert_run(project, "run-1").status is ConversionStatus.EXISTS
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


@pytest.mark.parametrize("with_old_event", [False, True])
def test_empty_and_pretrace_journals_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_old_event: bool
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    if with_old_event:
        journal.emit(CoreEventType.RUN_STARTED)
    else:
        Project.event_journal_path_for(project.root, "run-1").touch()
    result = convert_run(project, "run-1")
    assert result.status is ConversionStatus.SKIPPED
    assert ("predates" if with_old_event else "no events") in result.reason
    assert not Project.run_log_path_for(project.root, "run-1").exists()


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
        convert_run(project, "run-1")
    assert destination.read_bytes() == torn
    source.write_bytes(original)
    assert convert_run(project, "run-1").status is ConversionStatus.CONVERTED


def test_corrupt_destination_is_reported_without_overwriting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    destination = Project.run_log_path_for(project.root, "run-1")
    destination.write_bytes(b"BADMAGIC")
    with pytest.raises(ValueError, match="byte 0"):
        convert_run(project, "run-1")
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
    with FileRunLogWriter(destination) as writer:
        writer.append(Record(seq=1, epoch=1, body=Oneof("epoch_started", EpochStarted())))
        writer.append(Record(seq=2, epoch=1, body=Oneof("legacy_event", LegacyEvent(json="old"))))
    original = Project.event_journal_path_for(project.root, "run-1").read_bytes()
    assert convert_run(project, "run-1").status is ConversionStatus.CONVERTED
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
    assert convert_run(project, "run-1").status is ConversionStatus.EXISTS


@pytest.mark.parametrize("empty_header", [False, True])
def test_conversion_repairs_a_tee_with_no_event_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, empty_header: bool
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    destination = Project.run_log_path_for(project.root, "run-1")
    with FileRunLogWriter(destination) as writer:
        if empty_header:
            writer.append(Record(seq=1, epoch=1, body=Oneof("epoch_started", EpochStarted())))
    assert convert_run(project, "run-1").status is ConversionStatus.CONVERTED
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
        assert convert_run(project, "run-1").status is ConversionStatus.CONVERTED
        destination = Project.run_log_path_for(project.root, "run-1")
        with destination.open("rb") as stream:
            records = list(RunLogReader(stream))
        state = MappingState("run-1", vibesys_version=version("vibesys"))
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
        assert convert_run(project, "run-1").status is ConversionStatus.EXISTS
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
    convert_run(project, "run-1")
    directory = Project.log_directory_for(project.root, "run-1")
    original = (directory / "run.log").read_bytes()
    if missing in {"head", "both"}:
        (directory / "run.head").unlink()
    if missing in {"keyframes", "both"}:
        for path in (directory / "keyframes").iterdir():
            path.unlink()
    result = convert_run(project, "run-1")
    assert result.status is ConversionStatus.CONVERTED
    assert "repaired" in result.reason
    head = RunHead.from_binary((directory / "run.head").read_bytes())
    seed = Keyframe.from_binary((directory / "keyframes" / f"{head.committed_seq}.pb").read_bytes())
    assert seed.log_offset == len(original)
    assert (directory / "run.log").read_bytes() == original
    assert convert_run(project, "run-1").status is ConversionStatus.EXISTS


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
    result = convert_run(project, "run-1")
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
    rows = {row.run: row.result for row in convert_runs(project)}
    assert rows["run-1"].status is ConversionStatus.CONVERTED
    assert rows["bad"].status is ConversionStatus.FAILED
    assert rows["empty"].status is ConversionStatus.SKIPPED
    assert rows["old"].status is ConversionStatus.SKIPPED
    assert "unsupported journal event" in rows["old"].reason
    assert "predates" in rows["pretrace"].reason
    assert rows[str(external_link)].status is ConversionStatus.CONVERTED
    assert rows[str(index / "broken")].status is ConversionStatus.FAILED
    assert rows[str(index / "unsupported")].status is ConversionStatus.SKIPPED
    assert "already processed" in rows[str(index / "run-1")].reason
    assert len(rows) == 9
    second = {row.run: row.result for row in convert_runs(project)}
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
    rows = convert_runs(project)
    assert len(rows) == 1
    assert rows[0].run == "store-only"
    assert rows[0].result.status is ConversionStatus.SKIPPED
    assert "state.pb" in rows[0].result.reason
    assert namespace.read_bytes("store.json") == b"original core state"


def test_gateway_conversion_fixture_regenerates_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = journal_at(tmp_path, monkeypatch)
    directory = Path(__file__).parents[3] / "proto/testdata/conversion"
    generated = runpy.run_path(str(directory / "generate.py"))["fixtures"](project)
    for name, payload in generated.items():
        saved = (directory / name).read_bytes()
        if name.endswith(".gz"):
            assert gzip.decompress(saved) == gzip.decompress(payload), name
        else:
            assert saved == payload, name


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
    convert_run(project, "run-1")
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
            convert_run(project, "run-1")
    elif damage == "keyframe-offset":
        path = directory / "keyframes" / f"{head.committed_seq}.pb"
        seed = Keyframe.from_binary(path.read_bytes())
        seed.log_offset += 1
        path.write_bytes(seed.to_binary())
        with pytest.raises(ValueError, match="checkpoint does not match"):
            convert_run(project, "run-1")
    else:
        head.committed_seq -= 1
        (directory / "run.head").write_bytes(head.to_binary())
        assert convert_run(project, "run-1").status is ConversionStatus.CONVERTED
        assert convert_run(project, "run-1").status is ConversionStatus.EXISTS


def test_bulk_reports_index_publication_failure_and_preserves_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    record_trace(journal)
    home = tmp_path / "home"
    (home / "runs").mkdir()
    (home / "runs/run-1").write_bytes(b"keep")
    results = convert_runs(project)
    assert results[0].result.status is ConversionStatus.FAILED
    assert "not a symlink" in results[0].result.reason
    assert results[1].result.status is ConversionStatus.SKIPPED
    assert (home / "runs/run-1").read_bytes() == b"keep"
