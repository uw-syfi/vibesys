"""The contract suite every :class:`~vs_logs.api.LogStore` implementation passes.

A test module subclasses :class:`LogStoreContract` as ``Test<Variant>`` and implements
:meth:`LogStoreContract.store_under_test`. Cases draw from seeded randomness.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, TypeAdapter

from vs_logs._core import FromOffset, LogLimits, Tail
from vs_logs._reader import LogChunk, LogReader
from vs_logs._records import INDEX_NAME, StreamOutcome
from vs_sim.api import OutputStream, SeededRandom

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_logs._store import LogStore, LogStream

_CASES = 20


class ContractOwner(BaseModel):
    """The owner model the contract cases open streams for."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str


CONTRACT_OWNER: TypeAdapter[ContractOwner] = TypeAdapter(ContractOwner)


@dataclass(frozen=True)
class LogStoreUnderTest:
    """A store, a reader over its files, and the writer-side operations a case needs."""

    store: LogStore[ContractOwner]
    reader: LogReader[ContractOwner]
    reopen: Callable[[], LogStore[ContractOwner]]
    """A new store instance over the same files, as a restarted writer opens it."""


def no_warning(message: str) -> None:
    """The ``warn`` of a store whose volume never fails: a warning fails the test."""
    raise AssertionError(message)


def read_everything(reader: LogReader[ContractOwner], stream_id: str, max_bytes: int) -> LogChunk:
    """Read a stream from its earliest retained byte to its end in ``max_bytes`` steps."""
    first = reader.read(stream_id, FromOffset(0), max_bytes)
    data = bytearray(first.data)
    pipes = list(first.pipes)
    chunk = first
    while not chunk.eof:
        chunk = reader.read(stream_id, FromOffset(chunk.next_offset), max_bytes)
        data += chunk.data
        pipes.extend(chunk.pipes)
    return LogChunk(
        stream_id=stream_id,
        offset=first.offset,
        data=bytes(data),
        pipes=tuple(pipes),
        truncated_before=first.truncated_before,
        eof=True,
        closed=chunk.closed,
    )


def bytes_by_pipe(chunk: LogChunk) -> dict[OutputStream, bytes]:
    """Each pipe's bytes in a chunk, in order."""
    found = dict.fromkeys(OutputStream, b"")
    for span in chunk.pipes:
        begin = span.offset - chunk.offset
        found[span.pipe] += chunk.data[begin : begin + span.length]
    return found


def _writes(rng: SeededRandom) -> list[tuple[OutputStream, bytes]]:
    writes = []
    for _ in range(rng.randint(0, 12)):
        data = bytes(rng.randint(0, 255) for _ in range(rng.randint(0, 30)))
        writes.append((rng.choice(list(OutputStream)), data))
    return writes


class LogStoreContract:
    """Cases for :class:`~vs_logs.api.LogStore`. Implement :meth:`store_under_test`."""

    def store_under_test(self, limits: LogLimits) -> LogStoreUnderTest:
        """A fresh, empty store under ``limits``."""
        raise NotImplementedError

    def test_written_bytes_read_back_in_any_windows_with_their_pipes(self) -> None:
        """Uncapped, every byte reads back at its offset, labeled with the pipe that wrote it."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            subject = self.store_under_test(LogLimits(max_bytes=1 << 20, segment_bytes=7))
            writes = _writes(rng)
            with subject.store.open(ContractOwner(name="w"), "cmd") as stream:
                for pipe, data in writes:
                    stream.write(pipe, data)
            chunk = read_everything(subject.reader, stream.stream_id, rng.randint(1, 20))
            assert chunk.data == b"".join(data for _, data in writes), seed
            expected = {pipe: b"".join(d for p, d in writes if p is pipe) for pipe in OutputStream}
            assert bytes_by_pipe(chunk) == expected, seed
            assert sum(span.length for span in chunk.pipes) == len(chunk.data), seed

    def test_the_cap_keeps_a_suffix_and_offsets_only_move_forward(self) -> None:
        """Eviction keeps the newest bytes at their offsets; the retained start never falls."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            limits = LogLimits(max_bytes=rng.randint(4, 40), segment_bytes=1)
            limits = LogLimits(limits.max_bytes, rng.randint(1, limits.max_bytes))
            subject = self.store_under_test(limits)
            written = b""
            retained_start = 0
            with subject.store.open(ContractOwner(name="capped"), "cmd") as stream:
                for pipe, data in _writes(rng):
                    stream.write(pipe, data)
                    written += data
                    (info,) = subject.reader.list_streams()
                    assert info.size == len(written), seed
                    assert info.retained_start >= retained_start, seed
                    assert info.size - info.retained_start <= limits.max_bytes, seed
                    retained_start = info.retained_start
            chunk = read_everything(subject.reader, stream.stream_id, rng.randint(1, 9))
            assert chunk.offset == retained_start, seed
            assert chunk.data == written[retained_start:], seed
            assert chunk.truncated_before == (retained_start > 0), seed

    def test_reads_at_or_past_the_end_are_empty_and_eof(self) -> None:
        """Past the end there is nothing yet, and the reader says so."""
        subject = self.store_under_test(LogLimits(max_bytes=64, segment_bytes=8))
        with subject.store.open(ContractOwner(name="end"), "cmd") as stream:
            stream.write(OutputStream.STDOUT, b"hello")
            for offset in (5, 6, 1000):
                chunk = subject.reader.read(stream.stream_id, FromOffset(offset), 10)
                assert (chunk.data, chunk.eof, chunk.closed) == (b"", True, False)
        tail = subject.reader.read(stream.stream_id, Tail(3), 10)
        assert (tail.offset, tail.data, tail.eof, tail.closed) == (2, b"llo", True, True)

    def test_a_closed_stream_reports_its_outcome(self) -> None:
        """Leaving normally ends a stream, leaving by an exception fails it, close keeps the first."""
        subject = self.store_under_test(LogLimits())
        with subject.store.open(ContractOwner(name="ok"), "true") as ended:
            ended.write(OutputStream.STDOUT, b"x")
        failing = subject.store.open(ContractOwner(name="bad"), "false")
        with contextlib.suppress(_ExpectedError):
            _fail_inside(failing)
        with subject.store.open(ContractOwner(name="chosen"), "cmd") as chosen:
            chosen.close(StreamOutcome.FAILED)
        outcomes = {info.owner.name: info.outcome for info in subject.reader.list_streams()}
        assert outcomes == {
            "ok": StreamOutcome.ENDED,
            "bad": StreamOutcome.FAILED,
            "chosen": StreamOutcome.FAILED,
        }
        assert subject.reader.read(ended.stream_id, FromOffset(0), 8).closed

    def test_writing_after_close_is_dropped(self) -> None:
        """A closed stream takes no more bytes, and the late writer never sees an error."""
        subject = self.store_under_test(LogLimits())
        with subject.store.open(ContractOwner(name="done"), "cmd") as stream:
            stream.write(OutputStream.STDOUT, b"kept")
        stream.write(OutputStream.STDOUT, b"late")
        stream.close(StreamOutcome.FAILED)
        (info,) = subject.reader.list_streams()
        assert (info.size, info.outcome, info.degraded) == (4, StreamOutcome.ENDED, False)

    def test_a_reopened_store_abandons_open_streams_and_numbers_on(self) -> None:
        """A restarted writer closes what its predecessor left open and never reuses an id."""
        subject = self.store_under_test(LogLimits())
        left_open = subject.store.open(ContractOwner(name="crashed"), "cmd")
        left_open.write(OutputStream.STDERR, b"partial")
        reopened = subject.reopen()
        with reopened.open(ContractOwner(name="next"), "cmd") as fresh:
            pass
        infos = subject.reader.list_streams()
        assert [info.outcome for info in infos] == [StreamOutcome.ABANDONED, StreamOutcome.ENDED]
        assert len({left_open.stream_id, fresh.stream_id}) == 2
        assert subject.reader.read(left_open.stream_id, Tail(100), 100).data == b"partial"

    def test_a_torn_last_index_line_is_cut_when_the_store_reopens(self) -> None:
        """A short index write (a full disk) never corrupts the records written after it."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            subject = self.store_under_test(LogLimits())
            with subject.store.open(ContractOwner(name="before"), "cmd") as before:
                before.write(OutputStream.STDOUT, b"x")
            left_open = subject.store.open(ContractOwner(name="crashed"), "cmd")
            torn = b'{"kind":"opened","stream_id":"000003","owner":{"name":"lost"}}\n'
            subject.store.volume.append(INDEX_NAME, torn[: rng.randint(1, len(torn) - 1)])
            with subject.reopen().open(ContractOwner(name="after"), "cmd") as after:
                after.write(OutputStream.STDERR, b"y")
            listed = [(info.owner.name, info.outcome) for info in subject.reader.list_streams()]
            assert listed == [
                ("before", StreamOutcome.ENDED),
                ("crashed", StreamOutcome.ABANDONED),
                ("after", StreamOutcome.ENDED),
            ], seed
            assert len({before.stream_id, left_open.stream_id, after.stream_id}) == 3, seed


class _ExpectedError(Exception):
    pass


def _fail_inside(stream: LogStream) -> None:
    """Leave ``stream``'s block by an exception, as failing work does."""
    with stream:
        raise _ExpectedError
