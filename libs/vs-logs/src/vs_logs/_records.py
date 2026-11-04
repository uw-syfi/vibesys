"""The durable index and pipe-mark records, and the typed listing built from them.

The index (``index.jsonl``) is append-only: one ``opened`` record per stream, then at most
one ``closed`` record. A stream's pipe marks (``<stream>/pipes.jsonl``) record where the
writing pipe changed. A reader ignores a trailing line without its newline: the writer is
still appending it, or a short write (a full disk) left it torn. A store that opens the
index cuts such a line first (:func:`complete_length`), so its own records start on a
line of their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from vs_decode.api import Unreadable, decode_json
from vs_logs._core import MarkPipe
from vs_sim.api import OutputStream

if TYPE_CHECKING:
    from vs_sim.api import ByteSource

INDEX_NAME = "index.jsonl"
PIPES_NAME = "pipes.jsonl"
SEGMENT_SUFFIX = ".seg"
_SEGMENT_DIGITS = 20
_MAX_RECORD_BYTES = 1024 * 1024
_READ_BYTES = 1024 * 1024

StreamId = Annotated[str, Field(pattern=r"^[0-9]{6,12}$")]
"""A stream's name in its store: digits only, so it is always one safe path component."""
_STREAM_ID = TypeAdapter(StreamId)


def parse_stream_id(value: str) -> str:
    """Return ``value`` as a stream id, or raise ``ValueError`` naming it."""
    try:
        return _STREAM_ID.validate_python(value)
    except ValidationError as error:
        message = f"not a log stream id: {value!r}"
        raise ValueError(message) from error


def stream_id_for(ordinal: int) -> str:
    """The id of the ``ordinal``-th stream opened in a store."""
    return f"{ordinal:06d}"


def segment_name(stream_id: str, start: int) -> str:
    """The name of the segment of ``stream_id`` whose first byte is at ``start``."""
    return f"{stream_id}/{start:0{_SEGMENT_DIGITS}d}{SEGMENT_SUFFIX}"


def segment_start(name: str) -> int | None:
    """The start offset a segment file name encodes, or ``None`` for any other file."""
    stem, suffix = name[: -len(SEGMENT_SUFFIX)], name[-len(SEGMENT_SUFFIX) :]
    if suffix != SEGMENT_SUFFIX or len(stem) != _SEGMENT_DIGITS or not stem.isdigit():
        return None
    return int(stem)


class CorruptLogError(Exception):
    """A store's index or pipe marks hold a record that does not decode."""


class StreamOutcome(StrEnum):
    """How a stream ended."""

    ENDED = "ended"
    """Its writer closed it normally."""
    FAILED = "failed"
    """Its writer closed it because the work it records failed."""
    ABANDONED = "abandoned"
    """Its writer stopped without closing it; a later store found it open."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OpenedRecord(_Record):
    """A stream was opened for an owner, to record a command's output."""

    kind: Literal["opened"] = "opened"
    stream_id: StreamId
    owner: JsonValue
    command: str
    adopted_path: None = Field(default=None, exclude=True)
    """Always null: stores before 2026-10-16 wrote it, so their indexes still decode."""


class ClosedRecord(_Record):
    """A stream was closed: no byte will be added to it."""

    kind: Literal["closed"] = "closed"
    stream_id: StreamId
    outcome: StreamOutcome
    degraded: bool = False
    """The store failed to write some of the stream's bytes, so the stream lacks them.

    Written only when true, so a stream that lost nothing is recorded as before.
    """


type IndexRecord = Annotated[OpenedRecord | ClosedRecord, Field(discriminator="kind")]
_INDEX_RECORD: TypeAdapter[IndexRecord] = TypeAdapter(IndexRecord)


class PipeRecord(_Record):
    """From offset ``at`` on, the bytes were written by ``pipe``."""

    at: int = Field(ge=0)
    pipe: OutputStream


def encode_index(record: OpenedRecord | ClosedRecord) -> bytes:
    """One index line, with its newline."""
    quiet = isinstance(record, ClosedRecord) and not record.degraded
    return record.model_dump_json(exclude={"degraded"} if quiet else None).encode() + b"\n"


def encode_pipe(mark: MarkPipe) -> bytes:
    """One pipe-mark line, with its newline."""
    return PipeRecord(at=mark.at, pipe=mark.pipe).model_dump_json().encode() + b"\n"


def read_all(source: ByteSource) -> bytes:
    """Every byte ``source`` holds now."""
    parts: list[bytes] = []
    offset = 0
    while chunk := source.read(offset, _READ_BYTES):
        parts.append(chunk)
        offset += len(chunk)
    return b"".join(parts)


def complete_length(data: bytes) -> int:
    """How many bytes of ``data`` its complete lines take: up to and with its last newline."""
    return data.rfind(b"\n") + 1


def _complete_lines(data: bytes) -> list[bytes]:
    return [line for line in data[: complete_length(data)].split(b"\n") if line]


def decode_index(data: bytes, *, where: str) -> list[OpenedRecord | ClosedRecord]:
    """The complete records of an index, failing loudly on a corrupt one."""
    records: list[OpenedRecord | ClosedRecord] = []
    for number, line in enumerate(_complete_lines(data), start=1):
        decoded = decode_json(
            line, _INDEX_RECORD, source=f"{where}:{number}", max_bytes=_MAX_RECORD_BYTES
        )
        if isinstance(decoded, Unreadable):
            message = f"unreadable log index record: {decoded}"
            raise CorruptLogError(message)
        records.append(decoded)
    return records


def decode_pipes(data: bytes, *, where: str) -> tuple[MarkPipe, ...]:
    """The complete pipe marks of a stream."""
    marks: list[MarkPipe] = []
    for number, line in enumerate(_complete_lines(data), start=1):
        decoded = decode_json(
            line, PipeRecord, source=f"{where}:{number}", max_bytes=_MAX_RECORD_BYTES
        )
        if isinstance(decoded, Unreadable):
            message = f"unreadable pipe mark: {decoded}"
            raise CorruptLogError(message)
        marks.append(MarkPipe(at=decoded.at, pipe=decoded.pipe))
    return tuple(marks)


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One stream as the index records it."""

    opened: OpenedRecord
    closed: StreamOutcome | None
    degraded: bool = False
    """Its close recorded that the store lost some of its bytes."""


def fold_index(records: list[OpenedRecord | ClosedRecord]) -> dict[str, IndexEntry]:
    """The streams an index lists, in the order they were opened."""
    entries: dict[str, IndexEntry] = {}
    for record in records:
        match record:
            case OpenedRecord():
                if record.stream_id in entries:
                    message = f"log stream {record.stream_id} is opened twice in the index"
                    raise CorruptLogError(message)
                entries[record.stream_id] = IndexEntry(record, None)
            case ClosedRecord():
                entry = entries.get(record.stream_id)
                if entry is None:
                    message = f"log stream {record.stream_id} is closed but was never opened"
                    raise CorruptLogError(message)
                entries[record.stream_id] = IndexEntry(
                    entry.opened, record.outcome, record.degraded
                )
    return entries
