"""The ``LogReader`` and ``InstanceLogs`` contract the host helper's ``logs`` feature relies on, for every implementation.

``FakeLogReader`` (the in-memory Fake the helper's tests use) and ``StoreLogReader`` (the
real reader over a run's ``vs_logs`` store, in a temporary directory) run the same cases.
A subject caps each stream's retained bytes the way its store does: the Fake keeps exactly
the newest ``cap`` bytes, ``vs_logs`` evicts whole segments; the cases hold for both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from pydantic import JsonValue, TypeAdapter
from tests.server.host_helper_support import FakeLogReader

from server.host_api import (
    HelperError,
    HelperErrorCode,
    LogOffset,
    LogPipe,
    LogPipeSpan,
    LogsListRequest,
    LogsReadRequest,
    LogStreamOutcome,
    LogTail,
)
from server.host_helper_logs import (
    HelperLogs,
    LogReader,
    RunLocation,
    UnknownInstanceError,
    UnknownLogStreamError,
    list_logs,
    read_log,
)
from server.host_helper_os import StoreLogReader
from vs_logs.api import FileLogStore, LogLimits, LogStream, StreamOutcome
from vs_logs.api.testing import no_warning
from vs_sim.api import OutputStream
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_INSTANCE = "0123456789ab"
_UNKNOWN = "ffffffffffff"
_OWNER: dict[str, JsonValue] = {"kind": "startup_step", "step": "image_build"}


@dataclass(frozen=True)
class LogReaderUnderTest:
    """A reader of ``_INSTANCE``'s streams, and the means to record what it then reads."""

    reader: LogReader
    open: Callable[[], str]
    """Open a stream and return its id."""
    write: Callable[[str, bytes, LogPipe], None]
    close: Callable[[str, LogStreamOutcome], None]


class LogReaderContract:
    """What the helper relies on from every ``LogReader``; a subclass names the implementation."""

    def subject(self, cap: int | None = None) -> LogReaderUnderTest:
        """A fresh reader; with ``cap``, each stream retains at most about ``cap`` bytes."""
        raise NotImplementedError

    def test_reads_from_an_offset_at_most_max_bytes(self) -> None:
        subject = self.subject()
        stream = subject.open()
        subject.write(stream, b"0123456789", LogPipe.STDOUT)
        stored = subject.reader.instance(_INSTANCE).read(stream, LogOffset(offset=3), 4)
        assert (stored.offset, stored.data, stored.eof, stored.closed) == (3, b"3456", False, False)
        assert not stored.truncated_before

    def test_a_read_past_the_end_returns_no_bytes_and_reaches_the_end(self) -> None:
        subject = self.subject()
        stream = subject.open()
        subject.write(stream, b"abc", LogPipe.STDOUT)
        stored = subject.reader.instance(_INSTANCE).read(stream, LogOffset(offset=3), 4)
        assert (stored.offset, stored.data, stored.eof) == (3, b"", True)

    def test_a_tail_reads_the_last_bytes_and_says_when_the_stream_closed(self) -> None:
        subject = self.subject()
        stream = subject.open()
        subject.write(stream, b"0123456789", LogPipe.STDOUT)
        subject.close(stream, LogStreamOutcome.ENDED)
        tail = subject.reader.instance(_INSTANCE).read(stream, LogTail(tail_bytes=3), 64)
        assert (tail.offset, tail.data, tail.eof, tail.closed) == (7, b"789", True, True)

    def test_reads_of_evicted_bytes_start_at_the_oldest_held(self) -> None:
        subject = self.subject(cap=64)
        stream = subject.open()
        written = b"".join(b"line %03d\n" % line for line in range(40))
        for start in range(0, len(written), 9):
            subject.write(stream, written[start : start + 9], LogPipe.STDOUT)
        (listed,) = subject.reader.instance(_INSTANCE).streams()
        assert 0 < listed.retained_start < listed.size == len(written)
        stored = subject.reader.instance(_INSTANCE).read(stream, LogOffset(offset=0), 16)
        assert (stored.offset, stored.truncated_before) == (listed.retained_start, True)
        assert stored.data == written[stored.offset : stored.offset + 16]
        tail = subject.reader.instance(_INSTANCE).read(stream, LogTail(tail_bytes=10), 64)
        assert (tail.offset, tail.data, tail.truncated_before) == (
            len(written) - 10,
            written[-10:],
            False,
        )

    def test_lists_streams_oldest_first_with_their_state(self) -> None:
        subject = self.subject()
        first = subject.open()
        second = subject.open()
        subject.write(second, b"xy", LogPipe.STDERR)
        subject.close(first, LogStreamOutcome.FAILED)
        listed = subject.reader.instance(_INSTANCE).streams()
        assert [(s.id, s.owner, s.size, s.outcome) for s in listed] == [
            (first, _OWNER, 0, LogStreamOutcome.FAILED),
            (second, _OWNER, 2, None),
        ]

    def test_labels_each_byte_with_the_pipe_that_wrote_it(self) -> None:
        subject = self.subject()
        stream = subject.open()
        subject.write(stream, b"out\n", LogPipe.STDOUT)
        subject.write(stream, b"err\n", LogPipe.STDERR)
        stored = subject.reader.instance(_INSTANCE).read(stream, LogOffset(offset=2), 64)
        assert stored.pipes == (
            LogPipeSpan(offset=2, length=2, pipe=LogPipe.STDOUT),
            LogPipeSpan(offset=4, length=4, pipe=LogPipe.STDERR),
        )

    def test_logs_opened_once_see_every_later_write_and_the_close(self) -> None:
        subject = self.subject()
        stream = subject.open()
        opened = subject.reader.instance(_INSTANCE)
        assert opened.read(stream, LogOffset(offset=0), 64).data == b""
        subject.write(stream, b"late\n", LogPipe.STDOUT)
        subject.close(stream, LogStreamOutcome.ENDED)
        stored = opened.read(stream, LogOffset(offset=0), 64)
        assert (stored.data, stored.eof, stored.closed) == (b"late\n", True, True)
        assert [listed.size for listed in opened.streams()] == [5]

    def test_unknown_instances_and_streams_raise(self) -> None:
        subject = self.subject()
        subject.open()
        with pytest.raises(UnknownInstanceError):
            subject.reader.instance(_UNKNOWN)
        with pytest.raises(UnknownLogStreamError):
            subject.reader.instance(_INSTANCE).read("000099", LogOffset(offset=0), 4)


class TestFakeLogReader(LogReaderContract):
    def subject(self, cap: int | None = None) -> LogReaderUnderTest:
        store = FakeLogReader(SimThreads(schedule_seed=0))
        store.instances[_INSTANCE] = {}
        opened = [0]

        def open_stream() -> str:
            opened[0] += 1
            stream_id = f"{opened[0]:06d}"
            store.open(_INSTANCE, stream_id, _OWNER, "make")
            return stream_id

        def write(stream_id: str, data: bytes, pipe: LogPipe) -> None:
            store.write(_INSTANCE, stream_id, data, pipe)
            if cap is not None:
                (stream,) = (s for s in store.streams(_INSTANCE) if s.id == stream_id)
                store.evict(_INSTANCE, stream_id, stream.size - cap)

        return LogReaderUnderTest(
            reader=store,
            open=open_stream,
            write=write,
            close=lambda stream_id, outcome: store.close(_INSTANCE, stream_id, outcome),
        )


class TestStoreLogReader(LogReaderContract):
    @pytest.fixture(autouse=True)
    def _directory(self, tmp_path: Path) -> None:
        self._root = tmp_path

    def subject(self, cap: int | None = None) -> LogReaderUnderTest:
        directory = self._root / f"streams-{cap}"
        limits = None if cap is None else LogLimits(max_bytes=cap, segment_bytes=cap // 4)
        store = FileLogStore(
            directory,
            TypeAdapter(dict[str, JsonValue]),
            threads=SimThreads(schedule_seed=0),
            warn=no_warning,
            limits=limits,
        )
        location = RunLocation(project_root="/project", run_id="run-1")
        reader = StoreLogReader(
            lambda instance_id: location if instance_id == _INSTANCE else None,
            lambda _: directory,
        )
        streams: dict[str, LogStream] = {}

        def open_stream() -> str:
            stream = store.open(_OWNER, "make")
            streams[stream.stream_id] = stream
            return stream.stream_id

        return LogReaderUnderTest(
            reader=reader,
            open=open_stream,
            write=lambda stream_id, data, pipe: streams[stream_id].write(
                OutputStream(pipe.value), data
            ),
            close=lambda stream_id, outcome: streams[stream_id].close(StreamOutcome(outcome.value)),
        )


def test_a_store_whose_index_does_not_decode_is_a_typed_error_not_an_internal_one(
    tmp_path: Path,
) -> None:
    """A corrupt store answers with an error the client does not retry, on list and read."""
    (tmp_path / "index.jsonl").write_bytes(b'{"kind":"opened","stream_id":"000001"\n')
    location = RunLocation(project_root="/project", run_id="run-1")
    reader = StoreLogReader(
        lambda instance_id: location if instance_id == _INSTANCE else None, lambda _: tmp_path
    )
    clock = ManualClock()
    logs = HelperLogs(reader, clock)

    listed = list_logs(LogsListRequest(request_id="l", deadline_ms=1, instance_id=_INSTANCE), logs)
    read = read_log(
        LogsReadRequest(
            request_id="r",
            deadline_ms=10_000,
            instance_id=_INSTANCE,
            stream_id="000001",
            start=LogOffset(offset=0),
            max_bytes=64,
        ),
        logs,
        SimThreads().event(),  # never set: a corrupt store answers before any wait
    )

    for reply in (listed, read):
        assert isinstance(reply, HelperError), reply
        assert reply.code is HelperErrorCode.LOG_STORE_CORRUPT
