"""The read side: list a store's streams and read bounded chunks of one, over byte sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_logs._core import MarkPipe, PipeSpan, ReadStart, pipe_spans, plan_read
from vs_logs._records import (
    INDEX_NAME,
    PIPES_NAME,
    IndexEntry,
    StreamOutcome,
    decode_index,
    decode_pipes,
    fold_index,
    parse_stream_id,
    read_all,
    segment_name,
    segment_start,
)
from vs_logs._volume import DirectoryVolume, Volume
from vs_sim.api import OutputStream

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import TypeAdapter

    from vs_logs._store import LogStore
    from vs_sim.api import ByteSource

_ATTEMPTS = 3
"""Reads re-planned when the writer evicts a segment between listing and reading it."""


class UnknownStreamError(KeyError):
    """The store lists no stream with this id."""


@dataclass(frozen=True, slots=True)
class StreamInfo[OwnerT]:
    """One stream as a reader sees it now."""

    stream_id: str
    owner: OwnerT
    command: str
    retained_start: int
    """The earliest offset still retained; bytes before it were evicted."""
    size: int
    """Every byte ever written; the next byte gets this offset."""
    outcome: StreamOutcome | None
    """How it was closed, or ``None`` while it is open."""
    degraded: bool
    """The store failed to write some of its bytes (a full disk), so they are missing."""


@dataclass(frozen=True, slots=True)
class LogChunk:
    """Bytes ``[offset, offset + len(data))`` of a stream, labeled with their pipes."""

    stream_id: str
    offset: int
    data: bytes
    pipes: tuple[PipeSpan, ...]
    truncated_before: bool
    """Bytes before ``offset`` were asked for but are no longer retained."""
    eof: bool
    """Nothing after this chunk was there when it was read."""
    closed: bool
    """The stream was closed before this read began: with ``eof``, no byte will follow."""

    @property
    def next_offset(self) -> int:
        """Where the next read continues."""
        return self.offset + len(self.data)


@dataclass(frozen=True, slots=True)
class _Extent:
    retained_start: int
    size: int
    pieces: tuple[tuple[int, ByteSource], ...]
    """Each retained segment's start and source, oldest first."""


class LogReader[OwnerT]:
    """Lists and reads the streams of one store; never writes."""

    def __init__(self, volume: Volume, owner: TypeAdapter[OwnerT]) -> None:
        """Read ``volume``, decoding owners with ``owner``."""
        self._volume = volume
        self._owner = owner

    def list_streams(self) -> tuple[StreamInfo[OwnerT], ...]:
        """Every stream, in the order they were opened."""
        return tuple(self._info(entry) for entry in self._entries().values())

    def read(self, stream_id: str, start: ReadStart, max_bytes: int) -> LogChunk:
        """At most ``max_bytes`` of ``stream_id`` from ``start``.

        The close state is read before the bytes, so ``closed`` with ``eof`` means the
        chunk reaches the stream's final end.
        """
        stream_id = parse_stream_id(stream_id)
        for _ in range(_ATTEMPTS):
            entry = self._entry(stream_id)
            try:
                return self._read(entry, start, max_bytes)
            except FileNotFoundError:
                continue
        message = f"log stream {stream_id} kept changing while it was read"
        raise RuntimeError(message)

    def _read(self, entry: IndexEntry, start: ReadStart, max_bytes: int) -> LogChunk:
        stream_id = entry.opened.stream_id
        extent = self._extent(entry)
        window = plan_read(extent.retained_start, extent.size, start, max_bytes)
        data = _gather(extent.pieces, window.start, window.end)
        marks = self._marks(stream_id)
        return LogChunk(
            stream_id=stream_id,
            offset=window.start,
            data=data,
            pipes=pipe_spans(
                marks, window.start, window.start + len(data), default=OutputStream.STDOUT
            ),
            truncated_before=window.truncated_before,
            eof=window.start + len(data) >= extent.size,
            closed=entry.closed is not None,
        )

    def _entries(self) -> dict[str, IndexEntry]:
        try:
            data = read_all(self._volume.source(INDEX_NAME))
        except FileNotFoundError:
            return {}
        return fold_index(decode_index(data, where=INDEX_NAME))

    def _entry(self, stream_id: str) -> IndexEntry:
        entry = self._entries().get(stream_id)
        if entry is None:
            raise UnknownStreamError(stream_id)
        return entry

    def _info(self, entry: IndexEntry) -> StreamInfo[OwnerT]:
        for _ in range(_ATTEMPTS):
            try:
                extent = self._extent(entry)
                break
            except FileNotFoundError:
                continue
        else:
            message = f"log stream {entry.opened.stream_id} kept changing while it was listed"
            raise RuntimeError(message)
        return StreamInfo(
            stream_id=entry.opened.stream_id,
            owner=self._owner.validate_python(entry.opened.owner),
            command=entry.opened.command,
            retained_start=extent.retained_start,
            size=extent.size,
            outcome=entry.closed,
            degraded=entry.degraded,
        )

    def _extent(self, entry: IndexEntry) -> _Extent:
        stream_id = entry.opened.stream_id
        starts = sorted(
            start
            for name in self._volume.names(stream_id)
            if (start := segment_start(name)) is not None
        )
        if not starts:
            return _Extent(0, 0, ())
        newest = self._volume.source(segment_name(stream_id, starts[-1]))
        size = starts[-1] + newest.size()
        pieces = tuple(
            (start, self._volume.source(segment_name(stream_id, start))) for start in starts[:-1]
        )
        return _Extent(starts[0], size, (*pieces, (starts[-1], newest)))

    def _marks(self, stream_id: str) -> tuple[MarkPipe, ...]:
        name = f"{stream_id}/{PIPES_NAME}"
        try:
            return decode_pipes(read_all(self._volume.source(name)), where=name)
        except FileNotFoundError:
            return ()


def _gather(pieces: tuple[tuple[int, ByteSource], ...], start: int, end: int) -> bytes:
    """The bytes ``[start, end)`` across segments; short only if a segment is shorter."""
    parts: list[bytes] = []
    cursor = start
    bounds = [piece_start for piece_start, _ in pieces[1:]] + [end] if pieces else []
    for (piece_start, source), piece_end in zip(pieces, bounds, strict=True):
        while cursor < min(end, piece_end) and cursor >= piece_start:
            chunk = source.read(cursor - piece_start, min(end, piece_end) - cursor)
            if not chunk:
                return b"".join(parts)
            parts.append(chunk)
            cursor += len(chunk)
    return b"".join(parts)


def open_log_directory[OwnerT](directory: Path, owner: TypeAdapter[OwnerT]) -> LogReader[OwnerT]:
    """A reader over the store kept in ``directory`` by any process."""
    return LogReader(DirectoryVolume(directory), owner)


def reader_of[OwnerT](store: LogStore[OwnerT], owner: TypeAdapter[OwnerT]) -> LogReader[OwnerT]:
    """A reader over the files ``store`` writes (any implementation)."""
    return LogReader(store.volume, owner)
