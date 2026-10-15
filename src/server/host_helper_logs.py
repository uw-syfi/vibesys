"""The host helper's ``logs`` feature: a run's recorded log streams, served in bounded chunks.

``logs.list`` names an instance's streams; ``logs.read`` returns one chunk of one
stream, from a byte offset or from its tail, and at the end of an open stream waits
up to ``wait_ms`` for more (the long poll a follower keeps one of in flight).
``docs/contributing/desktop-connection.md`` ("Logs") owns the design.

The helper needs one role from the log store, :class:`LogReader`: list an instance's
streams and read raw bytes. Everything the client sees on top of that is decided
here, purely, in :func:`cut_chunk`: where a chunk ends (after its last line end
when more bytes follow, else before a character the window splits), where a tail
starts (the first line start in it), and how bytes become text (UTF-8, invalid
bytes replaced). :func:`list_logs` and :func:`read_log` answer the two requests;
the long poll re-reads on the injected clock and ends before the request's deadline.

Light like the rest of the helper: the standard library, Pydantic, and
``server.host_api``.

Public surface: ``LogReader``, ``StoredBytes``, ``UnknownInstanceError``,
``UnknownLogStreamError``, ``HelperLogs``, ``cut_chunk``, ``list_logs``,
``read_log``, and the limits ``POLL_INTERVAL_SECONDS`` and ``REPLY_MARGIN_MS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from server.host_api import (
    HelperError,
    HelperErrorCode,
    LogChunk,
    LogOffset,
    LogsListReply,
    LogsListRequest,
    LogsReadReply,
    LogsReadRequest,
    LogStream,
    LogTail,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Clock

POLL_INTERVAL_SECONDS: Final = 0.25
"""How often a waiting ``logs.read`` re-reads the stream; new bytes reach the client at most this late."""
REPLY_MARGIN_MS: Final = 1000
"""How long before the request's deadline a waiting read gives up, so its reply beats the deadline."""

_NEWLINE: Final = b"\n"
_CONTINUATION_MASK: Final = 0b1100_0000
_CONTINUATION: Final = 0b1000_0000
_MAX_CHARACTER_BYTES: Final = 4
_LEAD_BYTES: Final = ((0b1111_0000, 4), (0b1110_0000, 3), (0b1100_0000, 2))
"""The smallest lead byte of each multi-byte UTF-8 character length, longest first."""


class UnknownInstanceError(Exception):
    """The store recorded no logs for this instance."""


class UnknownLogStreamError(Exception):
    """The instance has no stream with this id."""


@dataclass(frozen=True)
class StoredBytes:
    """Raw bytes ``[offset, offset + len(data))`` of a stream, as the store holds them now."""

    offset: int
    data: bytes
    end: int
    """How many bytes the stream holds now: the offset its next byte will have."""
    closed: bool
    truncated_before: bool
    """The read asked for bytes before ``offset`` that were evicted."""


class LogReader(Protocol):
    """The log store, as the helper reads it.

    Contract: ``streams`` lists every stream recorded for an instance, live or
    ended, oldest first. ``read`` returns at most ``max_bytes`` bytes and never
    blocks: from ``LogOffset`` it starts at that offset, or at the oldest byte
    still held when that offset was evicted (``truncated_before``), or returns no
    bytes at that offset when it is at or past the end; from ``LogTail`` it starts
    ``tail_bytes`` before the end, or at the oldest byte held. Both raise
    ``UnknownInstanceError`` for an instance with no logs, and ``read`` raises
    ``UnknownLogStreamError`` for a stream the instance does not have.
    """

    def streams(self, instance_id: str) -> tuple[LogStream, ...]:
        """The instance's streams, oldest first."""
        ...

    def read(
        self, instance_id: str, stream_id: str, start: LogOffset | LogTail, max_bytes: int
    ) -> StoredBytes:
        """At most ``max_bytes`` bytes of the stream from ``start``."""
        ...


@dataclass(frozen=True)
class HelperLogs:
    """What the ``logs`` feature needs from the host: the store and a clock to wait on."""

    reader: LogReader
    clock: Clock
    pause: Callable[[float], None]
    """Wait this many seconds on ``clock``'s timeline."""


def _complete_prefix(data: bytes) -> int:
    """The length of ``data`` without a final character its last bytes only begin."""
    for back in range(1, min(len(data), _MAX_CHARACTER_BYTES - 1) + 1):
        byte = data[-back]
        if byte & _CONTINUATION_MASK != _CONTINUATION:
            needed = _character_length(byte)
            return len(data) - back if needed > back else len(data)
    return len(data)


def _character_length(lead: int) -> int:
    return next((length for smallest, length in _LEAD_BYTES if lead >= smallest), 1)


def _line_start(data: bytes) -> int:
    """Where the first whole line in ``data`` begins, or the first character when none does."""
    newline = data.find(_NEWLINE)
    if 0 <= newline < len(data) - 1:
        return newline + 1
    skip = 0
    while skip < min(len(data), _MAX_CHARACTER_BYTES - 1) and (
        data[skip] & _CONTINUATION_MASK == _CONTINUATION
    ):
        skip += 1
    return skip


def cut_chunk(stored: StoredBytes, *, tail: bool) -> LogChunk:
    """The chunk a client receives for ``stored``: whole lines when more follow, whole characters always.

    A tail that does not start the stream starts at its first line start. A window
    more bytes follow ends after its last line end, or, with none, before a
    character it splits. A window at the end of an open stream holds back an
    unfinished last character; at the end of a closed stream nothing is held back,
    and an unfinished character decodes as U+FFFD.
    """
    data = stored.data
    offset = stored.offset
    if tail and offset > 0:
        skip = _line_start(data)
        data = data[skip:]
        offset += skip
    more = offset + len(data) < stored.end
    if more and _NEWLINE in data:
        data = data[: data.rfind(_NEWLINE) + 1]
    elif more or not stored.closed:
        data = data[: _complete_prefix(data)]
    return LogChunk(
        offset=offset,
        data=data.decode("utf-8", errors="replace"),
        next_offset=offset + len(data),
        eof=not more,
        closed=stored.closed,
        truncated_before=stored.truncated_before,
    )


def list_logs(request: LogsListRequest, logs: HelperLogs) -> LogsListReply | HelperError:
    """Answer ``logs.list``."""
    try:
        streams = logs.reader.streams(request.instance_id)
    except UnknownInstanceError:
        return _unknown_instance(request.request_id, request.instance_id)
    return LogsListReply(request_id=request.request_id, streams=streams)


def read_log(request: LogsReadRequest, logs: HelperLogs) -> LogsReadReply | HelperError:
    """Answer ``logs.read``; at the end of an open stream, wait for bytes until the wait ends.

    The wait is ``wait_ms``, cut to end ``REPLY_MARGIN_MS`` before the request's
    deadline; it re-reads every ``POLL_INTERVAL_SECONDS`` on ``logs.clock``.
    """
    wait_ms = min(request.wait_ms, max(request.deadline_ms - REPLY_MARGIN_MS, 0))
    until = logs.clock.now() + wait_ms / 1000
    tail = isinstance(request.start, LogTail)
    while True:
        try:
            stored = logs.reader.read(
                request.instance_id, request.stream_id, request.start, request.max_bytes
            )
        except UnknownInstanceError:
            return _unknown_instance(request.request_id, request.instance_id)
        except UnknownLogStreamError:
            message = f"instance {request.instance_id} has no log stream {request.stream_id!r}"
            return HelperError(
                request_id=request.request_id,
                code=HelperErrorCode.UNKNOWN_LOG_STREAM,
                message=message,
            )
        chunk = cut_chunk(stored, tail=tail)
        remaining = until - logs.clock.now()
        waiting = chunk.eof and not chunk.closed and chunk.next_offset == chunk.offset
        if not waiting or remaining <= 0:
            return LogsReadReply(request_id=request.request_id, chunk=chunk)
        logs.pause(min(POLL_INTERVAL_SECONDS, remaining))


def _unknown_instance(request_id: str, instance_id: str) -> HelperError:
    return HelperError(
        request_id=request_id,
        code=HelperErrorCode.UNKNOWN_INSTANCE,
        message=f"this host recorded no logs for instance {instance_id}",
    )
