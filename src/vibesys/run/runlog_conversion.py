"""One-shot conversion of the authoritative journal into the phase 1 transport."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from vibesys.events import CoreEvent, CoreEventType
from vibesys.run.runlog_tee import JOURNAL_EVENT_MAX_BYTES, RunLogTee
from vs_decode.api import Unreadable, decode_json
from vs_runlog.api import (
    FileRunContent,
    FileRunLogWriter,
    RunLogReader,
    RunLogStopReason,
    install_run_log,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_project.api import Project
    from vs_sim.api import SleepingClock, Threads


class RunLogConversionError(ValueError):
    """A journal or destination cannot be safely converted."""


class ConversionStatus(StrEnum):
    """Terminal outcomes of an idempotent conversion."""

    CONVERTED = "converted"
    EXISTS = "exists"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ConversionResult:
    """A human-reportable conversion outcome and its reason."""

    status: ConversionStatus
    reason: str


def _events(path: Path) -> Iterator[tuple[bytes, CoreEvent]]:
    with path.open("rb") as stream:
        while line := stream.readline(JOURNAL_EVENT_MAX_BYTES + 1):
            event = decode_json(
                line, CoreEvent, source=str(path), max_bytes=JOURNAL_EVENT_MAX_BYTES
            )
            if isinstance(event, Unreadable):
                raise RunLogConversionError(event.details("journal event"))
            yield line, event


def _already_complete(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("rb") as stream:
        reader = RunLogReader(stream)
        legacy = False
        typed = False
        for record in reader:
            body = record.body
            legacy |= body is not None and body.field == "legacy_event"
            typed |= body is not None and (
                body.field not in {"legacy_event", "epoch_started"}
                or (body.field == "epoch_started" and body.value.run is not None)
            )
    if reader.stop.reason is RunLogStopReason.CORRUPTION:
        message = f"corrupt run.log at byte {reader.stop.offset}: {reader.stop.detail}"
        raise ValueError(message)
    return reader.stop.reason is RunLogStopReason.END and not legacy and typed


def convert_run(
    project: Project, run_id: str, *, clock: SleepingClock, threads: Threads
) -> ConversionResult:
    """Atomically convert a quiescent run with trace events, preserving the JSONL source.

    Callers stop the run before converting. A complete destination is left byte
    for byte unchanged; an incomplete destination is replaced only after a full
    successful conversion. Corrupt destinations require operator attention.
    """
    destination = project.run_log_path_for(project.root, run_id)
    if _already_complete(destination):
        return ConversionResult(ConversionStatus.EXISTS, "run.log already exists and is complete")
    journal = project.event_journal_path_for(project.root, run_id)
    if not journal.exists():
        return ConversionResult(ConversionStatus.SKIPPED, "journal is missing")
    first = None
    has_trace = False
    for _, event in _events(journal):
        if first is None:
            first = event
        has_trace |= event.type in (
            CoreEventType.SPAN_STARTED,
            CoreEventType.SPAN_UPDATED,
            CoreEventType.SPAN_ENDED,
        )
    if first is None:
        return ConversionResult(ConversionStatus.SKIPPED, "journal has no events")
    if not has_trace:
        return ConversionResult(ConversionStatus.SKIPPED, "journal predates the run trace")
    with TemporaryDirectory(prefix=".convert-run-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "run.log"
        content = FileRunContent(
            project.run_blobs_directory_for(project.root, run_id),
            project.log_streams_directory_for(project.root, run_id),
        )
        with RunLogTee(
            FileRunLogWriter(staged, clock=clock, threads=threads), content, run_id
        ) as tee:
            for line, _ in _events(journal):
                tee.append(line)
        install_run_log(staged, destination)
    project.publish_run_for(project.root, run_id)
    return ConversionResult(ConversionStatus.CONVERTED, "converted journal to run.log")
