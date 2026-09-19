"""Append-only event store for the run journal.

The event contract itself is the generated ``server.wire.v2`` messages; this
module owns durable storage of them as JSONL, one canonical JSON object per
line, with lazy parsing and a sidecar offset index.
"""

from __future__ import annotations

import json
import threading
import uuid
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import TYPE_CHECKING, Any

from google.protobuf import json_format, struct_pb2
from pydantic import BaseModel

from server.event_index import (
    EventIndexRecord,
    load_event_index,
    source_stat,
    write_event_index,
)
from server.wire import PROTOCOL_VERSION, codec, messages, upgrade, validate
from server.wire.codec import WireError
from server.wire.v2 import events_pb2

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import BinaryIO

    from server.event_index import SourceStat

EventTypeValue = events_pb2.EventType.ValueType

_V1_TYPE_NUMBERS: dict[str, int] = {
    value.name.removeprefix("EVENT_TYPE_").lower(): value.number
    for value in events_pb2.EventType.DESCRIPTOR.values
    if value.number != 0
}
_V2_TYPE_NUMBERS: dict[str, int] = {
    value.name: value.number
    for value in events_pb2.EventType.DESCRIPTOR.values
    if value.number != 0
}


def parse_event(raw: str | bytes) -> events_pb2.RunEvent:
    """Parse and validate one journal line, upgrading a version 1 record.

    Raises :class:`WireError` for anything that is not a valid event, so a
    caller distinguishes corruption with one exception type.
    """
    try:
        record = json.loads(raw)
    except ValueError as error:
        raise WireError("invalid_json", f"Event is not valid JSON: {error}") from error
    if not isinstance(record, dict):
        raise WireError("invalid_message", "Event must be a JSON object")
    try:
        event = (
            upgrade.event_from_legacy(record)
            if upgrade.is_legacy(record)
            else codec.from_dict(events_pb2.RunEvent, record)
        )
    except (TypeError, AttributeError, KeyError) as error:
        raise WireError("invalid_message", f"Malformed event record: {error!r}") from error
    validate.validate_event(event)
    return event


_EAGER_TAIL_RECORDS = 1024
"""How many trailing records ``EventStore`` validates at construction.

The final record decides malformed-tail truncation, so it must be parsed
eagerly. Widening that to a window also keeps the common attach-then-read-the
-tail path free of any lazy parse, at a bounded cost on an empty run.
"""


@dataclass(frozen=True, slots=True)
class EventHeader:
    """Scalar identity of one stored record, recovered without full validation.

    ``sequence`` is the repaired cursor value ``read`` will report, not
    necessarily the integer on disk. ``execution_id`` already folds in the
    version 1 ``invocation_id`` field the same way the upgrade does.
    """

    sequence: int
    type: EventTypeValue
    execution_id: str | None
    chat_thread_id: str | None


_UNLOCATED = -1
"""Offset of a record that is only in memory, never read back from disk."""


@dataclass(slots=True)
class _StoredRecord:
    """One record's location on disk plus its parse, once something forces it.

    ``offset`` is ``_UNLOCATED`` for a record this process appended, and for
    every record on the eager fallback path: those already carry ``event``, so
    nothing ever asks the file for them again.
    """

    header: EventHeader
    offset: int
    length: int
    raw_sequence: int
    event: events_pb2.RunEvent | None = None


class EventStore:
    """Serialize event access so readers never observe partial JSONL writes.

    Read contract: reads return the stored ``RunEvent`` objects themselves, in
    a fresh list. Generated messages are mutable, so a reader must treat them
    as read-only and project history with :func:`server.wire.messages.replace`
    instead of mutating what it reads. Copying every event per read cost ~1.9s
    on a 72k-event history, paid again on each new subscription's full replay.

    Construction only scans the log with ``json.loads`` (measured ~2.6x cheaper
    than full validation) to learn each record's byte range and header fields,
    then validates the tail. Older records are validated when a read reaches
    them and cached from then on. Any doubt during the scan discards the index
    and falls back to validating the whole file, so a corrupt history still
    raises from ``__init__``: the worst case is a slow attach, never wrong
    state.
    """

    def __init__(self, path: Path, run_id: str):  # noqa: ANN204, D107  # tracked: #288
        self.path = path
        self.run_id = run_id
        # Names this store's sequence space. Sequences are only comparable
        # within one store, and a run replaces its store mid-flight when the
        # durable log is attached, so a consumer holding folded state needs an
        # identity to tell "the next events" from "a different log's events".
        # Neither ``path`` nor ``run_id`` can serve: a retired store can be
        # reopened at the same path, and ``run_id`` is reassigned in place.
        self.store_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._parsed_records = 0
        self._records, self._malformed_tail_offset = self._scan_unlocked()
        # A valid final record whose line was never terminated must gain its
        # newline before ``append`` writes anything after it.
        self._missing_tail_newline = self._malformed_tail_offset is None and _ends_without_newline(
            self.path
        )
        self._sequences = [record.header.sequence for record in self._records]
        self._next_sequence = self._sequences[-1] + 1 if self._sequences else 1

    def append(self, event: events_pb2.RunEvent) -> events_pb2.RunEvent:  # noqa: D102  # tracked: #288
        with self._changed:
            if self._malformed_tail_offset is not None:
                with self.path.open("r+b") as stream:
                    stream.truncate(self._malformed_tail_offset)
                self._malformed_tail_offset = None
            if self._missing_tail_newline:
                # Terminate the valid final record so the new record starts
                # its own line instead of concatenating onto it. The flag is a
                # construction-time observation, so recheck the file itself: if
                # it was removed or replaced since, a blind "\n" would corrupt
                # the fresh file's first record.
                if _ends_without_newline(self.path):
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write("\n")
                self._missing_tail_newline = False
            event = messages.replace(event, sequence=self._next_sequence, run_id=self.run_id)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(codec.dumps(event) + "\n")
            self._next_sequence += 1
            self._records.append(
                _StoredRecord(
                    header=header_from_event(event, event.sequence),
                    offset=_UNLOCATED,
                    length=0,
                    raw_sequence=event.sequence,
                    event=event,
                )
            )
            self._sequences.append(event.sequence)
            self._changed.notify_all()
            return event

    @property
    def last_sequence(self) -> int:  # noqa: D102  # tracked: #288
        with self._lock:
            return self._next_sequence - 1

    @property
    def parsed_record_count(self) -> int:
        """How many stored records have been validated into models so far.

        Accounting for callers that must assert an attach stayed lazy without
        resorting to timing.
        """
        with self._lock:
            return self._parsed_records

    def event_headers(self) -> list[EventHeader]:
        """Return every stored record's header, in replay order, unparsed.

        This is the whole log's shape at scan cost. Consumers that only need
        event types and identities (a lifecycle index) read it instead of
        forcing the history into models.
        """
        with self._lock:
            return [record.header for record in self._records]

    def read(  # noqa: D102  # tracked: #288
        self, after_sequence: int = 0, before_sequence: int | None = None
    ) -> list[events_pb2.RunEvent]:
        with self._lock:
            return self._events_after_unlocked(after_sequence, before_sequence)

    def read_sequences(self, sequences: Iterable[int]) -> list[events_pb2.RunEvent]:
        """Return the records at the given cursor values, in the order asked.

        Unknown sequences are skipped. Only the named records are validated,
        which is what lets a consumer inspect a handful of rare payloads
        without paying for the history around them.
        """
        with self._lock:
            records: list[_StoredRecord] = []
            for sequence in sequences:
                index = bisect_left(self._sequences, sequence)
                if index < len(self._sequences) and self._sequences[index] == sequence:
                    records.append(self._records[index])
            self._force_parse_unlocked(records)
            return [record.event for record in records if record.event is not None]

    def wait(self, after_sequence: int, timeout: float | None = None) -> list[events_pb2.RunEvent]:
        """Block until replayable events exist after a client's cursor."""
        with self._changed:
            events = self._events_after_unlocked(after_sequence)
            if events:
                return events
            self._changed.wait(timeout)
            return self._events_after_unlocked(after_sequence)

    def wait_for_change(self, after_sequence: int, timeout: float | None = None) -> bool:
        """Block until a record exists past the cursor; report it, parse nothing.

        A subscriber that only needs to know the stream moved must not pay to
        validate the window it moved by. On a resumed run that window is the
        entire durable history.
        """
        with self._changed:
            if self._next_sequence - 1 > after_sequence:
                return True
            self._changed.wait(timeout)
            return self._next_sequence - 1 > after_sequence

    def notify_change(self) -> None:
        """Wake every waiter without appending, for a store being retired.

        A waiter blocked on a store the run has replaced would otherwise sleep
        out its timeout before noticing that the store it should read is a
        different object.
        """
        with self._changed:
            self._changed.notify_all()

    def _events_after_unlocked(
        self, after_sequence: int, before_sequence: int | None = None
    ) -> list[events_pb2.RunEvent]:
        start = bisect_right(self._sequences, after_sequence)
        stop = (
            len(self._records)
            if before_sequence is None
            else bisect_left(self._sequences, before_sequence)
        )
        if stop <= start:
            return []
        # A bounded read must only force the records it returns; that is what
        # keeps a backfill query off the whole log.
        window = self._records[start:stop]
        self._force_parse_unlocked(window)
        # A new list, so callers own the sequence; the frozen events inside it
        # stay shared with the store.
        return [record.event for record in window if record.event is not None]

    def _force_parse_unlocked(self, records: list[_StoredRecord]) -> None:
        """Validate any of these records not yet in memory, in log order.

        Records adjacent on disk are fetched in one read, so a dense range
        costs one seek while a sparse targeted read costs one seek per record.
        """
        pending = [record for record in records if record.event is None]
        if not pending:
            return
        with self.path.open("rb") as stream:
            run: list[_StoredRecord] = []
            for record in pending:
                if run and record.offset != run[-1].offset + run[-1].length:
                    self._parse_run_unlocked(stream, run)
                    run = []
                run.append(record)
            self._parse_run_unlocked(stream, run)

    def _parse_run_unlocked(self, stream: BinaryIO, run: list[_StoredRecord]) -> None:
        base = run[0].offset
        stream.seek(base)
        blob = stream.read(run[-1].offset + run[-1].length - base)
        for record in run:
            begin = record.offset - base
            self._parse_record(record, blob[begin : begin + record.length])

    def _parse_record(self, record: _StoredRecord, raw: bytes) -> None:
        self._parsed_records += 1
        event = parse_event(raw)
        # The parse is fresh, so it is safe to set. Only a legacy out-of-order
        # or duplicate sequence differs; every other record is handed out
        # exactly as it was written.
        event.sequence = record.header.sequence
        record.event = event

    def _scan_unlocked(self) -> tuple[list[_StoredRecord], int | None]:
        """Index the log by byte range and header without a full-file allocation.

        A sidecar whose indexed prefix is unchanged restores the record index
        without parsing that prefix; a source that has only grown (the journal
        is append-only) has just its suffix scanned, and the extended index is
        republished. Otherwise this streams the whole source once and
        atomically publishes a replacement cache. A record the cheap header scan cannot
        classify is fully validated in place, so strict corruption detection
        does not require retaining the other event payloads.
        """
        if not self.path.exists():
            return [], None

        cached = load_event_index(self.path)
        if cached is not None:
            try:
                records: list[_StoredRecord] = []
                while cached.records:
                    records.append(_stored_record_from_index(cached.records.pop()))
                records.reverse()
            except ValueError:
                records = []
            else:
                initial_source = source_stat(self.path)
                records, malformed_tail_offset, safe_count, safe_boundary = (
                    self._scan_stream_unlocked(records, start=cached.boundary)
                )
                self._parse_eager_tail(records, malformed_tail_offset)
                if initial_source is not None and safe_boundary != cached.boundary:
                    self._publish_index(records, safe_count, safe_boundary, initial_source)
                return records, malformed_tail_offset

        initial_source = source_stat(self.path)
        records, malformed_tail_offset, safe_count, safe_boundary = self._scan_stream_unlocked(
            [], start=0
        )
        self._parse_eager_tail(records, malformed_tail_offset)
        if initial_source is not None:
            self._publish_index(records, safe_count, safe_boundary, initial_source)
        return records, malformed_tail_offset

    def _publish_index(
        self,
        records: list[_StoredRecord],
        safe_count: int,
        safe_boundary: int,
        initial_source: SourceStat,
    ) -> None:
        write_event_index(
            self.path,
            (_index_record_from_stored(records[index]) for index in range(safe_count)),
            safe_count,
            safe_boundary,
            initial_source,
        )

    def _scan_stream_unlocked(
        self, records: list[_StoredRecord], *, start: int
    ) -> tuple[list[_StoredRecord], int | None, int, int]:
        """Extend ``records`` by streaming source lines beginning at ``start``."""
        malformed_tail_offset: int | None = None
        last_sequence = records[-1].header.sequence if records else 0
        safe_count = len(records)
        safe_boundary = start
        for record_offset, line, is_final in _stream_lines(self.path, start=start):
            header_fields = _scan_header_fields(line)
            if header_fields is None:
                # A final line holding complete JSON the header scan cannot
                # classify must be judged by full validation, so it falls to
                # the eager path; only a tail with no complete JSON prefix (a
                # torn append) is set aside for repair.
                if is_final and not _starts_with_complete_json(line):
                    # Preserve access to earlier audit history if a process was
                    # interrupted during its final append.
                    malformed_tail_offset = record_offset
                    break
                try:
                    self._parsed_records += 1
                    event = parse_event(line)
                except WireError as error:
                    if not is_final:
                        raise
                    raise _complete_invalid_tail_error(self.path, record_offset) from error
                raw_sequence = event.sequence
                sequence = raw_sequence if raw_sequence > last_sequence else last_sequence + 1
                event.sequence = sequence
                last_sequence = sequence
                records.append(
                    _StoredRecord(
                        header=header_from_event(event, sequence),
                        offset=record_offset,
                        length=len(line),
                        raw_sequence=raw_sequence,
                        event=event,
                    )
                )
                if line.endswith(b"\n"):
                    safe_count = len(records)
                    safe_boundary = record_offset + len(line)
                continue
            raw_sequence, event_type, execution_id, chat_thread_id = header_fields
            sequence = raw_sequence if raw_sequence > last_sequence else last_sequence + 1
            last_sequence = sequence
            records.append(
                _StoredRecord(
                    header=EventHeader(
                        sequence=sequence,
                        type=event_type,
                        execution_id=execution_id,
                        chat_thread_id=chat_thread_id,
                    ),
                    offset=record_offset,
                    length=len(line),
                    raw_sequence=raw_sequence,
                )
            )
            if line.endswith(b"\n"):
                safe_count = len(records)
                safe_boundary = record_offset + len(line)
        return records, malformed_tail_offset, safe_count, safe_boundary

    def _parse_eager_tail(
        self, records: list[_StoredRecord], malformed_tail_offset: int | None
    ) -> None:
        """Validate the trailing window, raising on any record that fails.

        Every record here scanned as complete JSON, which a torn append can
        never leave behind, so a validation failure on the final record is
        corruption to surface, not an interrupted write to set aside.
        """
        start = max(0, len(records) - _EAGER_TAIL_RECORDS)
        tail = records[start:]
        if not tail:
            return
        with self.path.open("rb") as stream:
            base = tail[0].offset
            stream.seek(base)
            raw = stream.read(tail[-1].offset + tail[-1].length - base)
        for relative_position, record in enumerate(tail):
            begin = record.offset - base
            try:
                self._parse_record(record, raw[begin : begin + record.length])
            except WireError as error:
                position = start + relative_position
                if position != len(records) - 1 or malformed_tail_offset is not None:
                    raise
                raise _complete_invalid_tail_error(self.path, record.offset) from error

    def _read_unlocked(self) -> tuple[list[events_pb2.RunEvent], int | None]:
        if not self.path.exists():
            return [], None
        events: list[events_pb2.RunEvent] = []
        for record_offset, line, is_final in _stream_lines(self.path, start=0):
            try:
                self._parsed_records += 1
                event = parse_event(line)
                events.append(event)
            except WireError as error:
                if not is_final:
                    raise
                if _starts_with_complete_json(line):
                    raise _complete_invalid_tail_error(self.path, record_offset) from error
                # Preserve access to earlier audit history if a process was
                # interrupted during its final append.
                return events, record_offset
        return events, None


def _stream_lines(path: Path, *, start: int) -> Iterable[tuple[int, bytes, bool]]:
    """Yield source lines with offsets and final-line identity using bounded memory."""
    with path.open("rb") as stream:
        stream.seek(start)
        offset = start
        line = stream.readline()
        while line:
            following = stream.readline()
            yield offset, line, not following
            offset += len(line)
            line = following


def _stored_record_from_index(record: EventIndexRecord) -> _StoredRecord:
    """Restore a typed in-memory record from primitive validated cache fields."""
    return _StoredRecord(
        header=EventHeader(
            sequence=record.sequence,
            type=_type_from_index(record.event_type),
            execution_id=record.execution_id,
            chat_thread_id=record.chat_thread_id,
        ),
        offset=record.offset,
        length=record.length,
        raw_sequence=record.raw_sequence,
    )


def _type_from_index(name: str) -> EventTypeValue:
    """Map a sidecar's stored type name back to the enum, or raise ``ValueError``."""
    number = _V2_TYPE_NUMBERS.get(name)
    if number is None:
        raise ValueError(f"unknown event type {name!r} in event index")  # noqa: TRY003
    return number


def _index_record_from_stored(record: _StoredRecord) -> EventIndexRecord:
    """Project one scanned record into the sidecar's primitive representation."""
    return EventIndexRecord(
        offset=record.offset,
        length=record.length,
        raw_sequence=record.raw_sequence,
        sequence=record.header.sequence,
        event_type=events_pb2.EventType.Name(record.header.type),
        execution_id=record.header.execution_id,
        chat_thread_id=record.header.chat_thread_id,
    )


def _scan_header_fields(line: bytes) -> tuple[int, EventTypeValue, str | None, str | None] | None:
    """Recover one record's header fields cheaply, or None if anything is off.

    Understands both spellings: version 1 records carry a lower-case ``type``
    and fold ``invocation_id`` into the execution identity; version 2 records
    carry ``EVENT_TYPE_*`` names. Every rejection here (non-object record,
    non-integer ``sequence``, unknown ``type``, non-string identity, another
    protocol version) is a case where full parsing could disagree with the
    scan, so the caller must parse the record the strict way rather than guess.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    version = record.get("protocol_version", 1)
    # An absent ``sequence`` is the proto3 default of 0, as in a version 1 model.
    sequence = record.get("sequence", 0)
    # ``type is not int`` also rejects bool.
    if type(version) is not int or type(sequence) is not int or sequence < 0:
        return None
    name = record.get("type")
    if version == 1:
        event_type = _V1_TYPE_NUMBERS.get(name) if isinstance(name, str) else None
        execution_id = record.get("execution_id") or record.get("invocation_id") or None
    elif version == PROTOCOL_VERSION:
        event_type = _V2_TYPE_NUMBERS.get(name) if isinstance(name, str) else None
        execution_id = record.get("execution_id")
    else:
        return None
    chat_thread_id = record.get("chat_thread_id")
    if (
        event_type is None
        or not _is_optional_str(execution_id)
        or not _is_optional_str(chat_thread_id)
    ):
        return None
    return sequence, event_type, execution_id, chat_thread_id


def _is_optional_str(value: Any) -> bool:  # noqa: ANN401  # scanning untyped JSON
    return value is None or isinstance(value, str)


def _starts_with_complete_json(line: bytes) -> bool:
    """Whether the bytes begin with one complete JSON value.

    A torn append leaves a strict prefix of the record plus its newline, and
    no strict prefix of a serialized object contains a complete JSON value, so
    this discriminates an interrupted write from fully written bytes: a lone
    record, or complete records concatenated onto one line by a writer that
    never terminated it. Truncating the latter would erase durable facts, so
    it must be surfaced as corruption, not repaired as a torn tail.
    """
    try:
        text = line.decode()
    except UnicodeDecodeError:
        return False
    try:
        json.JSONDecoder().raw_decode(text)
    except ValueError:
        return False
    return True


def _ends_without_newline(path: Path) -> bool:
    """Whether the file's final byte leaves its last record unterminated."""
    if not path.exists():
        return False
    size = path.stat().st_size
    if size == 0:
        return False
    with path.open("rb") as stream:
        stream.seek(size - 1)
        return stream.read(1) != b"\n"


def _complete_invalid_tail_error(path: Path, offset: int) -> ValueError:
    """Corruption error for a fully written final record that fails validation.

    Unlike a torn append, the record is complete, so silently truncating it
    would erase a durable fact; the operator must inspect the file instead.
    """
    return ValueError(f"complete final record at byte offset {offset} in {path} failed validation")


def header_from_event(event: events_pb2.RunEvent, sequence: int | None = None) -> EventHeader:
    """Project a parsed event to its header, with ``sequence`` overriding the stored one."""
    return EventHeader(
        sequence=event.sequence if sequence is None else sequence,
        type=event.type,
        execution_id=event.execution_id if event.HasField("execution_id") else None,
        chat_thread_id=event.chat_thread_id if event.HasField("chat_thread_id") else None,
    )


def _records_from_events(events: list[events_pb2.RunEvent]) -> list[_StoredRecord]:
    """Wrap already-validated events as stored records with no disk location."""
    return [
        _StoredRecord(
            header=header_from_event(event, event.sequence),
            offset=_UNLOCATED,
            length=0,
            raw_sequence=event.sequence,
            event=event,
        )
        for event in events
    ]


def _repair_legacy_sequences(events: list[events_pb2.RunEvent]) -> list[events_pb2.RunEvent]:
    """Expose a stable, strictly increasing cursor without rewriting the audit log."""
    repaired: list[events_pb2.RunEvent] = []
    last_sequence = 0
    for event in events:
        repaired_event = (
            messages.replace(event, sequence=last_sequence + 1)
            if event.sequence <= last_sequence
            else event
        )
        repaired.append(repaired_event)
        last_sequence = repaired_event.sequence
    return repaired


def json_value(value: Any) -> Any:  # noqa: ANN401
    """Return a JSON-safe form of ``value``, falling back to ``repr``."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        return repr(value)


def to_value(value: Any) -> struct_pb2.Value:  # noqa: ANN401
    """Convert an arbitrary producer result to a protobuf ``Value``."""
    converted = struct_pb2.Value()
    json_format.ParseDict(json_value(value), converted)
    return converted
