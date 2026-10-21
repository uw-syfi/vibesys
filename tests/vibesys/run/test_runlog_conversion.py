"""Conversion is atomic and idempotent over a real journal and temporary home."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from vibesys.api.transport import ConversionStatus, convert_run
from vibesys.events import CoreEventType, SpanStartedData
from vibesys.run import EventJournal
from vs_core.api import Span, SpanStatus, SpanType
from vs_project.api import Project
from vs_proto.api import RUN_LOG_MAGIC
from vs_runlog.api import RunLogReader
from vs_sim.api.testing import ManualClock, SimThreads


def journal_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Project, EventJournal]:
    """Build a JSONL-only run, as an older checkout would have written."""
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    project = Project.open(root)
    journal = EventJournal(ManualClock(start=100), SimThreads())
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


def test_conversion_preserves_json_and_timestamps_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, journal = journal_at(tmp_path, monkeypatch)
    journal.emit(CoreEventType.RUN_STARTED, "start")
    record_trace(journal)
    journal.emit(CoreEventType.RUN_FINISHED, "finish")
    source = Project.event_journal_path_for(project.root, "run-1")
    original = source.read_bytes()
    assert convert_run(project, "run-1").status is ConversionStatus.CONVERTED
    destination = Project.run_log_path_for(project.root, "run-1")
    converted = destination.read_bytes()
    with destination.open("rb") as stream:
        records = list(RunLogReader(stream))
    assert [record.seq for record in records] == [1, 2, 3, 4]
    assert records[0].body is not None
    assert records[0].body.field == "epoch_started"
    assert [record.at.seconds for record in records if record.at is not None] == [100] * 4
    assert [
        record.body.value.json
        for record in records
        if record.body is not None and record.body.field == "legacy_event"
    ] == original.decode().splitlines()
    assert convert_run(project, "run-1").status is ConversionStatus.EXISTS
    assert destination.read_bytes() == converted
    assert source.read_bytes() == original
    assert sorted(path.name for path in destination.parent.iterdir()) == [
        "core-events.jsonl",
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
