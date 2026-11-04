"""Where a command's output goes as it is written: one small write interface.

A producer (a process runner, a container exec, a remote job follower) writes each chunk
of a command's stdout and stderr to an :class:`OutputSink` as it reads it, and knows
nothing else about where the bytes go. Recording, tailing and display are layers above.
:data:`DISCARD` is the explicit opt-out, :func:`tee` sends one stream to several sinks,
:class:`Capture` keeps a bounded copy in memory, and :class:`Lines` splits the bytes into
decoded lines for a parser.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType


class OutputStream(StrEnum):
    """One of a command's two output streams."""

    STDOUT = "stdout"
    STDERR = "stderr"


class OutputSink(Protocol):
    """Receives a command's output, chunk by chunk, as the command writes it.

    Contract:

    - Chunks of one stream arrive in the order the command wrote them, one call at a time.
    - Calls for different streams may arrive concurrently, from the threads that drain the
      stdout and stderr pipes. A sink that keeps state shared by both streams synchronizes
      it itself; the bytes of each call stay contiguous wherever the sink puts them.
    - Chunk boundaries carry no meaning: the same bytes written in any chunking are the
      same output. An empty chunk is allowed and means nothing.
    - ``write`` does not raise for any bytes on either stream; an exception from it is a
      programming error and aborts the producer.
    """

    def write(self, stream: OutputStream, data: bytes) -> None:
        """Take the next ``data`` the command wrote to ``stream``."""
        ...


class _Discard:
    """Drops everything: the sink for output nobody asked to see."""

    def write(self, stream: OutputStream, data: bytes) -> None:
        """Drop ``data``."""
        del stream, data

    def __repr__(self) -> str:
        return "DISCARD"


DISCARD: OutputSink = _Discard()
"""The explicit opt-out: output written here is dropped."""


class _Tee:
    def __init__(self, sinks: tuple[OutputSink, ...]) -> None:
        self._sinks = sinks

    def write(self, stream: OutputStream, data: bytes) -> None:
        for sink in self._sinks:
            sink.write(stream, data)


def tee(*sinks: OutputSink) -> OutputSink:
    """One sink that writes every chunk to each of ``sinks``, in the order given."""
    live = tuple(sink for sink in sinks if sink is not DISCARD)
    if not live:
        return DISCARD
    if len(live) == 1:
        return live[0]
    return _Tee(live)


class Capture:
    """Keeps the first ``limit`` bytes of each stream in memory and counts the rest.

    The limit applies to each stream on its own. Each stream's state is its own, so the
    two streams may be written concurrently.
    """

    def __init__(self, limit: int) -> None:
        """Keep at most ``limit`` bytes of each stream; raises ``ValueError`` when negative."""
        if limit < 0:
            message = f"a capture limit must not be negative, got {limit}"
            raise ValueError(message)
        self.limit = limit
        self._kept = {stream: bytearray() for stream in OutputStream}
        self._omitted = dict.fromkeys(OutputStream, 0)

    def write(self, stream: OutputStream, data: bytes) -> None:
        """Keep what still fits under the limit and count what does not."""
        kept = self._kept[stream]
        room = max(self.limit - len(kept), 0)
        kept += data[:room]
        self._omitted[stream] += max(len(data) - room, 0)

    def kept(self, stream: OutputStream) -> bytes:
        """The first bytes written to ``stream``, at most ``limit`` of them."""
        return bytes(self._kept[stream])

    def omitted(self, stream: OutputStream) -> int:
        """How many bytes written to ``stream`` after the first ``limit`` were dropped."""
        return self._omitted[stream]

    @property
    def stdout(self) -> bytes:
        """What was kept of stdout."""
        return self.kept(OutputStream.STDOUT)

    @property
    def stderr(self) -> bytes:
        """What was kept of stderr."""
        return self.kept(OutputStream.STDERR)


type LineCallback = Callable[[OutputStream, str], None]
"""Receives one complete line of one stream, decoded, without its line ending."""

DEFAULT_MAX_LINE_BYTES = 64 * 1024
"""The longest line :class:`Lines` holds by default; the rest of a longer line is dropped."""


class Lines:
    r"""Splits each stream into lines and hands each one, decoded, to ``on_line``.

    A line ends at ``\\n``; the ending and one ``\\r`` before it are removed, and the bytes
    are decoded as UTF-8 with undecodable bytes replaced. A line split across chunks is
    carried until it ends, so every chunking of the same bytes gives the same lines. Only
    the first ``max_line_bytes`` of a longer line are kept, which bounds the carry.

    The owner of a ``Lines`` calls :meth:`finish` once the producer is done (or uses it as a
    context manager), so a last line without an ending is delivered too. ``on_line`` is
    called on the thread that wrote the line's last chunk; lines of different streams may
    reach it concurrently, so it synchronizes any state both streams share.
    """

    def __init__(
        self, on_line: LineCallback, *, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
    ) -> None:
        """Deliver lines to ``on_line``, each cut to at most ``max_line_bytes`` bytes."""
        if max_line_bytes < 1:
            message = f"max_line_bytes must be positive, got {max_line_bytes}"
            raise ValueError(message)
        self._on_line = on_line
        self._max_line_bytes = max_line_bytes
        self._carry = {stream: bytearray() for stream in OutputStream}
        self._open = dict.fromkeys(OutputStream, False)

    def write(self, stream: OutputStream, data: bytes) -> None:
        """Deliver every line ``data`` completes and carry the unfinished rest."""
        start = 0
        while (end := data.find(b"\n", start)) >= 0:
            self._append(stream, data[start:end])
            self._emit(stream)
            start = end + 1
        self._append(stream, data[start:])

    def finish(self) -> None:
        """Deliver each stream's last line when it has no line ending."""
        for stream in OutputStream:
            if self._open[stream]:
                self._emit(stream)

    def __enter__(self) -> Lines:
        """Use as the sink of one producer run."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Deliver the unterminated last lines, whether or not the run raised."""
        del exc_type, exc, traceback
        self.finish()

    def _append(self, stream: OutputStream, data: bytes) -> None:
        if not data:
            return
        self._open[stream] = True
        carry = self._carry[stream]
        carry += data[: max(self._max_line_bytes - len(carry), 0)]

    def _emit(self, stream: OutputStream) -> None:
        carry = self._carry[stream]
        line = bytes(carry).removesuffix(b"\r").decode("utf-8", errors="replace")
        carry.clear()
        self._open[stream] = False
        self._on_line(stream, line)
