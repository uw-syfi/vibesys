"""One-shot migration of pre-schema runs to the typed run log.

TEMPORARY: this module, ``vibesys host convert-run(s)`` and their tests are deleted after the
transport flip (tracking issue: #3441). The product never converts on read; a run that has not
been migrated is listed as "old format". Run it once per project, offline::

    vibesys host convert-runs --project <project directory> [--reconvert]

Each converted run also brings its chat conversations along (``import_legacy_chat``), so a
thread of the preceding app is still there after the migration. Only the run histories that
exist need to migrate; it is not a general converter.
"""

from __future__ import annotations

import asyncio
import builtins
import json
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from vibesys.chat._legacy import import_legacy_chat
from vibesys.chat._session import ChatRuntime
from vibesys.events import INTERRUPTS, CoreEvent, CoreEventType, EventStatus
from vibesys.run.runlog_tee import JOURNAL_EVENT_MAX_BYTES, RunLogTee
from vs_decode.api import Unreadable, decode_json
from vs_project.api import Project, ProjectError
from vs_proto.api import Keyframe, RunHead, validate
from vs_runlog.api import (
    FileRunContent,
    FileRunLogWriter,
    RunLogReader,
    RunLogStopReason,
    install_run_log,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from vs_sim.api import SleepingClock, Threads


class RunLogConversionError(ValueError):
    """A journal or destination cannot be safely converted."""


class _UnsupportedJournalError(RunLogConversionError):
    """The journal names a historical event kind this build cannot interpret."""


class ConversionStatus(StrEnum):
    """Terminal outcomes of an idempotent conversion."""

    CONVERTED = "converted"
    EXISTS = "exists"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class ConversionResult:
    """A human-reportable conversion outcome and its reason."""

    status: ConversionStatus
    reason: str


def _events(path: Path) -> Generator[tuple[bytes, CoreEvent]]:
    with path.open("rb") as stream:
        while line := stream.readline(JOURNAL_EVENT_MAX_BYTES + 1):
            event = decode_json(
                line, CoreEvent, source=str(path), max_bytes=JOURNAL_EVENT_MAX_BYTES
            )
            if isinstance(event, Unreadable):
                if any(p.location == ("type",) and p.error_type == "enum" for p in event.problems):
                    message = f"unsupported journal event: {event.details()}"
                    raise _UnsupportedJournalError(message)
                raise RunLogConversionError(event.details("journal event"))
            yield line, event


def _with_agent_identity(line: bytes) -> bytes:
    """Name the agent of a turn span whose older journal gave it no identity.

    The first journals labelled a turn with its role and model only. Its role is the identity the
    older agent events already get (``vibesys.records._agents``), so a turn of such a journal
    counts under its role.
    """
    if b'"agent_turn"' not in line:
        return line
    event = json.loads(line)
    label = (event.get("data") or {}).get("span", {}).get("label")
    if label is None or label.get("kind") != "agent_turn":
        return line
    attrs = json.loads(label.get("attrs_json") or "{}")
    if "agent" in attrs or "role" not in attrs:
        return line
    label["attrs_json"] = json.dumps({"agent": attrs["role"], **attrs}, separators=(",", ":"))
    return json.dumps(event, separators=(",", ":")).encode() + b"\n"


def _is_interrupt(event: CoreEvent) -> bool:
    """Whether a pre-schema run ended by an interrupt or a cancellation, which it recorded as a failure.

    The journal names the exception's class (``Name: message``). The class decides, as it does
    for a native run (``INTERRUPTS``), not a list of names.
    """
    if event.type is not CoreEventType.RUN_FAILED or event.data is not None:
        return False
    name = event.text.partition(":")[0]
    named = getattr(builtins, name, None) or getattr(asyncio, name, None)
    return isinstance(named, type) and issubclass(named, INTERRUPTS)


def _journal_line(line: bytes, event: CoreEvent) -> bytes:
    """The journal line to convert: an interrupted run ends Stopped, as a native one does."""
    if _is_interrupt(event):
        stopped = event.model_copy(
            update={
                "type": CoreEventType.STOPPED,
                "text": "Run stopped by interrupt",
                "status": EventStatus.INTERRUPTED,
            }
        )
        line = stopped.model_dump_json().encode() + b"\n"
    return _with_agent_identity(line)


def _written_by_migration(directory: Path) -> bool:
    """Whether a complete log can be rebuilt from its journal.

    A live writer knows its checkout; an offline conversion never does. A log that names one
    holds facts its journal cannot reproduce.
    """
    if not (directory / "core-events.jsonl").exists():
        return False
    with (directory / "run.log").open("rb") as stream:
        return not any(
            r.body is not None and r.body.field == "epoch_started" and r.body.value.provenance_known
            for r in RunLogReader(stream)
        )


def _complete_sequence(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as stream:
        reader = RunLogReader(stream)
        legacy = False
        typed = False
        sequence = 0
        for record in reader:
            if record.seq != sequence + 1:
                message = f"non-dense run.log sequence: {record.seq} after {sequence}"
                raise RunLogConversionError(message)
            sequence = record.seq
            body = record.body
            legacy |= body is not None and body.field == "legacy_event"
            typed |= body is not None and (
                body.field not in {"legacy_event", "epoch_started"}
                or (body.field == "epoch_started" and body.value.run is not None)
            )
    if reader.stop.reason is RunLogStopReason.CORRUPTION:
        message = f"corrupt run.log at byte {reader.stop.offset}: {reader.stop.detail}"
        raise ValueError(message)
    return sequence if reader.stop.reason is RunLogStopReason.END and not legacy and typed else 0


def _has_checkpoint(directory: Path, sequence: int) -> bool:
    path = directory / "keyframes" / f"{sequence}.pb"
    if not path.exists():
        return False
    frame = Keyframe.from_binary(path.read_bytes())
    validate(frame)
    if frame.through_seq != sequence or frame.log_offset != (directory / "run.log").stat().st_size:
        message = f"checkpoint does not match run.log: {path}"
        raise RunLogConversionError(message)
    needs_head = bool(frame.lifecycles and any(v.epoch and v.epoch.run for v in frame.epochs))
    head_path = directory / "run.head"
    if not head_path.exists():
        return not needs_head
    head = RunHead.from_binary(head_path.read_bytes())
    validate(head)
    return head.committed_seq == sequence


def convert_run(
    project: Project,
    run_id: str,
    *,
    clock: SleepingClock,
    threads: Threads,
    reconvert: bool = False,
) -> ConversionResult:
    """Convert one stopped project run; leave its journal and core store untouched.

    Core envelopes remain the resume authority until phase 3 introduces state.pb.
    A read-path conversion needs only the trace journal, not strategy state codecs.
    """
    directory = project.log_directory_for(project.root, run_id)
    try:
        result = _convert_directory(
            directory, run_id, clock=clock, threads=threads, reconvert=reconvert
        )
    except _UnsupportedJournalError as exc:
        return ConversionResult(ConversionStatus.SKIPPED, str(exc))
    if result.status in {ConversionStatus.CONVERTED, ConversionStatus.EXISTS}:
        project.publish_run_for(project.root, run_id)
        imported = import_legacy_chat(str(project.root), run_id, ChatRuntime(clock, threads))
        if imported:
            result = ConversionResult(
                result.status, f"{result.reason}; imported {len(imported)} chat threads"
            )
    if project.state.state_store_namespace(run_id).read_bytes("store.json") is not None:
        result = ConversionResult(
            result.status, result.reason + "; store.json retained, state.pb conversion is phase 3"
        )
    return result


def _convert_directory(
    directory: Path, run_id: str, *, clock: SleepingClock, threads: Threads, reconvert: bool
) -> ConversionResult:
    """Atomically convert a quiescent run with trace events, preserving the JSONL source.

    Callers stop the run before converting. A complete destination is left byte
    for byte unchanged; an incomplete destination is replaced only after a full
    successful conversion. Corrupt destinations require operator attention.
    """
    destination = directory / "run.log"
    sequence = _complete_sequence(destination)
    if sequence and not (reconvert and _written_by_migration(directory)):
        if _has_checkpoint(directory, sequence):
            return ConversionResult(ConversionStatus.EXISTS, "already converted")
        with FileRunLogWriter(destination, clock=clock, threads=threads):
            pass
        return ConversionResult(ConversionStatus.CONVERTED, "repaired run.head and keyframes")
    journal = directory / "core-events.jsonl"
    if not journal.exists():
        return ConversionResult(ConversionStatus.SKIPPED, "journal is missing")
    with closing(_events(journal)) as events:
        empty = next(events, None) is None
    if empty:
        return ConversionResult(ConversionStatus.SKIPPED, "journal has no events")
    with TemporaryDirectory(prefix=".convert-run-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "run.log"

        def prepare() -> None:
            content = FileRunContent(directory / "blobs", directory / "streams")
            writer = FileRunLogWriter(staged, clock=clock, threads=threads)
            with RunLogTee(writer, content, run_id) as tee:
                for line, event in _events(journal):
                    tee.append(_journal_line(line, event))
                writer.checkpoint()

        install_run_log(staged, destination, prepare=prepare)
    return ConversionResult(ConversionStatus.CONVERTED, "converted journal to run.log")


@dataclass(frozen=True)
class RunConversion:
    """One discovered run, including failures so a batch never silently drops one."""

    run: str
    result: ConversionResult


def _attempt(
    directory: Path, run_id: str, *, clock: SleepingClock, threads: Threads, reconvert: bool
) -> ConversionResult:
    try:
        return _convert_directory(
            directory, run_id, clock=clock, threads=threads, reconvert=reconvert
        )
    except _UnsupportedJournalError as exc:
        return ConversionResult(ConversionStatus.SKIPPED, str(exc))
    except (OSError, ValueError, TypeError, ProjectError) as exc:
        return ConversionResult(ConversionStatus.FAILED, str(exc))


def _indexed_target(entry: Path) -> Path | ConversionResult:
    try:
        if not entry.is_symlink():
            return ConversionResult(
                ConversionStatus.SKIPPED, "unsupported: index entry is not a symlink"
            )
        target = entry.resolve(strict=True)
        if not target.is_dir():
            return ConversionResult(ConversionStatus.FAILED, "index target is not a directory")
    except (OSError, RuntimeError) as exc:
        return ConversionResult(ConversionStatus.FAILED, f"unreadable index target: {exc}")
    else:
        return target


def convert_runs(
    project: Project, *, clock: SleepingClock, threads: Threads, reconvert: bool = False
) -> tuple[RunConversion, ...]:
    """Convert stopped project runs and every host index entry, once per target.

    ``reconvert`` rebuilds a run the migration already converted from its untouched journal, so
    a later improvement of the migration reaches it. Originals are never deleted, including after
    the seven-day retention window.
    A failed run does not prevent later runs from being attempted. Index entries
    without a supported journal are explicitly reported, including dangling links.
    """
    entries = [(entry, _indexed_target(entry)) for entry in project.indexed_run_directories()]
    results = []
    seen: set[Path] = set()
    ids = set(project.logged_run_ids_for(project.root)) | set(project.state.checkout_run_ids())
    for run_id in sorted(ids):
        directory = project.log_directory_for(project.root, run_id)
        seen.add(directory.resolve())
        try:
            result = convert_run(project, run_id, clock=clock, threads=threads, reconvert=reconvert)
        except (OSError, ValueError, TypeError, ProjectError) as exc:
            result = ConversionResult(ConversionStatus.FAILED, str(exc))
        if (
            result.status is ConversionStatus.SKIPPED
            and not (directory / "core-events.jsonl").exists()
        ):
            result = ConversionResult(
                ConversionStatus.SKIPPED,
                "unsupported: no trace journal; store.json core state requires phase 3 state.pb",
            )
        if result.status is ConversionStatus.EXISTS:
            result = ConversionResult(ConversionStatus.SKIPPED, result.reason)
        results.append(RunConversion(run_id, result))
    for entry, target in entries:
        if isinstance(target, ConversionResult):
            result = target
        elif target in seen:
            result = ConversionResult(
                ConversionStatus.SKIPPED, "already processed this index target"
            )
        else:
            result = _attempt(target, entry.name, clock=clock, threads=threads, reconvert=reconvert)
            seen.add(target)
        if result.status is ConversionStatus.EXISTS:
            result = ConversionResult(ConversionStatus.SKIPPED, result.reason)
        results.append(RunConversion(str(entry), result))
    return tuple(results)
