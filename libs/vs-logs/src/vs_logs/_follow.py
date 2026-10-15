"""Copy a growing source into a sink as it grows: the shell around :func:`follow_step`."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_logs._core import follow_step
from vs_sim.api import ByteSourceChangedError, OutputStream

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import ByteSource, OutputSink, Threads

DEFAULT_POLL_SECONDS = 1.0
DEFAULT_CHUNK_BYTES = 64 * 1024


class FollowEnd(StrEnum):
    """Why following stopped."""

    STOPPED = "stopped"
    """``stop`` asked, and every byte there was then has been copied."""
    CHANGED = "changed"
    """The source shrank or was replaced; copying on would mix two different logs."""


@dataclass(frozen=True, slots=True)
class FollowResult:
    """How following ended and how many bytes were copied."""

    end: FollowEnd
    copied: int


def follow(
    source: ByteSource,
    sink: OutputSink,
    *,
    threads: Threads,
    stop: Callable[[], bool],
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> FollowResult:
    """Copy ``source`` into ``sink`` until ``stop()`` or the source changes.

    A followed file has no pipes, so its bytes are written as ``STDOUT``.

    A source that does not exist yet is waited for. ``stop`` is checked before each pass,
    and the pass that sees it still copies everything there is, so bytes written before
    the stop was asked are not lost. Waits between passes are ``threads.sleep``, on the
    injected timeline.
    """
    have = 0
    while True:
        stopping = stop()
        have, changed = _drain(source, sink, have)
        if changed:
            return FollowResult(FollowEnd.CHANGED, have)
        if stopping:
            return FollowResult(FollowEnd.STOPPED, have)
        threads.sleep(poll_seconds)


def _drain(source: ByteSource, sink: OutputSink, have: int) -> tuple[int, bool]:
    """Copy what the source holds now; return the bytes copied so far and whether it changed."""
    try:
        size = source.size()
        while (fetch := follow_step(have, size, DEFAULT_CHUNK_BYTES)) is not None:
            data = source.read(fetch.offset, fetch.max_bytes)
            if not data:
                break
            sink.write(OutputStream.STDOUT, data)
            have += len(data)
    except FileNotFoundError:
        return have, False
    except ByteSourceChangedError:
        return have, True
    return have, False
