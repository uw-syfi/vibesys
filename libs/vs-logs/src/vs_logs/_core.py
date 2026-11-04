"""The pure core of a log stream: offsets, segments, the cap, and read windows.

Nothing here does I/O. The writer shell asks :func:`append` what to write and delete, and
the reader shell asks :func:`plan_read` which bytes to fetch and :func:`pipe_spans` which
pipe wrote them.

A stream's bytes are addressed by a monotonic offset from 0. They are kept in segments,
each named by the offset of its first byte; only the newest segment grows. When the
retained bytes exceed the cap, the oldest whole segments are dropped, so the earliest
retained offset only moves forward and every retained byte keeps its offset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_sim.api import OutputStream

DEFAULT_MAX_BYTES = 64 * 1024 * 1024
"""The default cap on one stream's retained bytes."""
_SEGMENTS_PER_CAP = 4


@dataclass(frozen=True, slots=True)
class LogLimits:
    """How many bytes one stream retains, and how big a segment grows before the next starts.

    Retained bytes never exceed ``max_bytes``; eviction drops a whole segment at a time, so
    at least ``max_bytes - segment_bytes`` of the newest bytes are kept.
    """

    max_bytes: int = DEFAULT_MAX_BYTES
    segment_bytes: int = DEFAULT_MAX_BYTES // _SEGMENTS_PER_CAP

    def __post_init__(self) -> None:
        """Reject limits under which the newest segment could itself exceed the cap."""
        if not 1 <= self.segment_bytes <= self.max_bytes:
            message = (
                f"segment_bytes must be between 1 and max_bytes ({self.max_bytes}), "
                f"got {self.segment_bytes}"
            )
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Segment:
    """``length`` retained bytes starting at offset ``start``."""

    start: int
    length: int

    @property
    def end(self) -> int:
        """The offset just past this segment."""
        return self.start + self.length


@dataclass(frozen=True, slots=True)
class StreamLayout:
    """Where a stream's retained bytes are, and which pipe wrote the last of them."""

    segments: tuple[Segment, ...] = ()
    size: int = 0
    """The offset the next byte gets: every byte ever written, retained or not."""
    pipe: OutputStream | None = None

    @property
    def retained_start(self) -> int:
        """The earliest offset still retained (``size`` when nothing is)."""
        return self.segments[0].start if self.segments else self.size


@dataclass(frozen=True, slots=True)
class MarkPipe:
    """Record that the bytes from offset ``at`` on were written by ``pipe``."""

    at: int
    pipe: OutputStream


@dataclass(frozen=True, slots=True)
class AppendBytes:
    """Append ``chunk[chunk_from:chunk_to]`` to the segment that starts at ``segment``."""

    segment: int
    chunk_from: int
    chunk_to: int


@dataclass(frozen=True, slots=True)
class DropSegment:
    """Delete the segment that starts at ``segment``: its bytes are evicted."""

    segment: int


@dataclass(frozen=True, slots=True)
class ForgetPipesBefore:
    """Pipe marks before ``offset`` are no longer needed, except the last of them."""

    offset: int


type StoreRequest = MarkPipe | AppendBytes | DropSegment | ForgetPipesBefore
"""What the writer shell does to the stream's files, in order."""


def append(
    layout: StreamLayout, limits: LogLimits, pipe: OutputStream, length: int
) -> tuple[StreamLayout, tuple[StoreRequest, ...]]:
    """Plan appending a ``length``-byte chunk from ``pipe``.

    The pipe mark comes before the bytes it labels, so a reader never sees bytes whose pipe
    is not yet recorded. Evictions come after the append that caused them.
    """
    if length < 0:
        message = f"a chunk length must not be negative, got {length}"
        raise ValueError(message)
    if length == 0:
        return layout, ()
    requests: list[StoreRequest] = []
    if pipe is not layout.pipe:
        requests.append(MarkPipe(at=layout.size, pipe=pipe))
    segments = list(layout.segments)
    written = 0
    while written < length:
        if not segments or segments[-1].length >= limits.segment_bytes:
            segments.append(Segment(start=layout.size + written, length=0))
        newest = segments[-1]
        taken = min(limits.segment_bytes - newest.length, length - written)
        requests.append(AppendBytes(newest.start, written, written + taken))
        segments[-1] = Segment(newest.start, newest.length + taken)
        written += taken
    retained = sum(segment.length for segment in segments)
    evicted = False
    while retained > limits.max_bytes:
        oldest = segments.pop(0)
        retained -= oldest.length
        requests.append(DropSegment(oldest.start))
        evicted = True
    if evicted:
        requests.append(ForgetPipesBefore(segments[0].start))
    return StreamLayout(tuple(segments), layout.size + length, pipe), tuple(requests)


@dataclass(frozen=True, slots=True)
class FromOffset:
    """Read from ``offset`` on."""

    offset: int

    def __post_init__(self) -> None:
        """Reject a negative offset."""
        if self.offset < 0:
            message = f"a read offset must not be negative, got {self.offset}"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Tail:
    """Read the last ``bytes`` bytes (or as many as ``max_bytes`` allows)."""

    bytes: int

    def __post_init__(self) -> None:
        """Reject a negative tail."""
        if self.bytes < 0:
            message = f"a tail length must not be negative, got {self.bytes}"
            raise ValueError(message)


type ReadStart = FromOffset | Tail


@dataclass(frozen=True, slots=True)
class ReadWindow:
    """The offsets ``[start, end)`` to fetch, and whether asked-for bytes were evicted."""

    start: int
    end: int
    truncated_before: bool
    """Bytes before ``start`` were asked for but are no longer retained."""
    eof: bool
    """``end`` is the stream's size when planned: nothing more is there yet."""


def plan_read(retained_start: int, size: int, start: ReadStart, max_bytes: int) -> ReadWindow:
    """Choose the window to read from a stream retaining ``[retained_start, size)``."""
    if max_bytes < 1:
        message = f"a read must ask for at least one byte, got {max_bytes}"
        raise ValueError(message)
    if not 0 <= retained_start <= size:
        message = f"retained start {retained_start} is outside [0, {size}]"
        raise ValueError(message)
    match start:
        case FromOffset(offset=offset):
            wanted = offset
        case Tail(bytes=tail):
            wanted = max(0, size - min(tail, max_bytes))
    begin = max(wanted, retained_start)
    end = max(begin, min(size, begin + max_bytes))
    return ReadWindow(begin, end, truncated_before=wanted < retained_start, eof=end >= size)


@dataclass(frozen=True, slots=True)
class PipeSpan:
    """``length`` bytes from ``offset`` were written by ``pipe``."""

    offset: int
    length: int
    pipe: OutputStream


def pipe_spans(
    marks: tuple[MarkPipe, ...], start: int, end: int, *, default: OutputStream
) -> tuple[PipeSpan, ...]:
    """Label ``[start, end)`` with the pipes ``marks`` record (``default`` before any mark)."""
    spans: list[PipeSpan] = []
    current = default
    cursor = start
    for mark in sorted(marks, key=lambda mark: mark.at):
        if mark.at <= start:
            current = mark.pipe
            continue
        if mark.at >= end:
            break
        if mark.pipe is not current:
            spans.append(PipeSpan(cursor, mark.at - cursor, current))
            cursor = mark.at
            current = mark.pipe
    if end > cursor:
        spans.append(PipeSpan(cursor, end - cursor, current))
    return tuple(spans)


def kept_marks(marks: tuple[MarkPipe, ...], offset: int) -> tuple[MarkPipe, ...]:
    """The marks still needed once bytes before ``offset`` are gone."""
    ordered = sorted(marks, key=lambda mark: mark.at)
    before = [mark for mark in ordered if mark.at <= offset]
    return tuple(before[-1:] + [mark for mark in ordered if mark.at > offset])
