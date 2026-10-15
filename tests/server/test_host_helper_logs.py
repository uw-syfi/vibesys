"""The host helper's ``logs`` feature: chunk cutting, tails, the long poll, and the gate.

Reads run through :func:`read_log` and :func:`list_logs` over ``FakeLogReader``, which
:class:`LogReaderContract` holds to the ``LogReader`` contract; the vs_logs adapter
joins that suite when it lands. The long poll runs through the channel on
``SimThreads``, so its waits are virtual time.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.host_helper_support import (
    ByteBuffer,
    ChannelClient,
    FakeLogReader,
    fake_host,
    serve_on,
    with_logs,
)

from server.host_api import (
    LOGS_READ_MAX_BYTES,
    LOGS_READ_MIN_BYTES,
    HelperError,
    HelperErrorCode,
    LogChunk,
    LogOffset,
    LogsListReply,
    LogsListRequest,
    LogsReadReply,
    LogsReadRequest,
    LogTail,
)
from server.host_helper_logs import (
    POLL_INTERVAL_SECONDS,
    REPLY_MARGIN_MS,
    HelperLogs,
    LogReader,
    UnknownInstanceError,
    UnknownLogStreamError,
    list_logs,
    read_log,
)
from vs_sim.api.testing import HANG_GUARD_S, ManualClock, SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable

_INSTANCE = "0123456789ab"
_STREAM = "build"
_OWNER: dict[str, Any] = {"kind": "startup_step", "step": "image_build"}

# Text with one-, two-, three- and four-byte characters and line ends.
_TEXT = st.text(alphabet=st.sampled_from(["a", "\n", "é", "€", "😀", " "]), max_size=200)
_READ_SIZES = st.integers(LOGS_READ_MIN_BYTES, 64)


def _store() -> FakeLogReader:
    clock = ManualClock(1_700_000_000.0)
    store = FakeLogReader(clock, SimThreads(schedule_seed=0))
    store.open(_INSTANCE, _STREAM, _OWNER, "docker build .")
    return store


def _logs(store: FakeLogReader) -> HelperLogs:
    clock = ManualClock()
    return HelperLogs(store, clock, clock.advance)


def _read(
    logs: HelperLogs,
    start: LogOffset | LogTail,
    max_bytes: int,
    *,
    stream_id: str = _STREAM,
    instance_id: str = _INSTANCE,
) -> LogsReadReply | HelperError:
    request = LogsReadRequest(
        request_id="r",
        deadline_ms=10_000,
        instance_id=instance_id,
        stream_id=stream_id,
        start=start,
        max_bytes=max_bytes,
    )
    return read_log(request, logs)


def _chunk(reply: LogsReadReply | HelperError) -> LogChunk:
    assert isinstance(reply, LogsReadReply), reply
    return reply.chunk


def _follow(logs: HelperLogs, offset: int, max_bytes: int) -> tuple[str, int, list[LogChunk]]:
    """Read from ``offset`` until a chunk reaches the end; the text, where it ended, the chunks."""
    text = []
    chunks = []
    while True:
        chunk = _chunk(_read(logs, LogOffset(offset=offset), max_bytes))
        chunks.append(chunk)
        assert chunk.offset == offset
        text.append(chunk.data)
        offset = chunk.next_offset
        if chunk.eof:
            return "".join(text), offset, chunks


# ----- cutting chunks -----


@settings(max_examples=examples(pr=200, nightly=2000))
@given(st.lists(st.binary(max_size=40), max_size=8), _READ_SIZES)
def test_reading_a_closed_stream_in_chunks_decodes_like_reading_it_whole(
    writes: list[bytes], max_bytes: int
) -> None:
    store = _store()
    for data in writes:
        store.write(_INSTANCE, _STREAM, data)
    store.close(_INSTANCE, _STREAM)
    text, end, chunks = _follow(_logs(store), 0, max_bytes)
    whole = b"".join(writes)
    assert text == whole.decode("utf-8", errors="replace")
    assert end == len(whole)
    for chunk in chunks:
        assert chunk.next_offset - chunk.offset <= max_bytes
        assert chunk.eof or chunk.next_offset > chunk.offset


@settings(max_examples=examples(pr=200, nightly=2000))
@given(_TEXT, st.lists(st.integers(0, 800), max_size=6), _READ_SIZES)
def test_a_follower_sees_every_character_once_however_the_writes_split_it(
    text: str, cuts: list[int], max_bytes: int
) -> None:
    data = text.encode()
    bounds = sorted({0, len(data), *(cut % (len(data) + 1) for cut in cuts)})
    store = _store()
    logs = _logs(store)
    seen = ""
    offset = 0
    for low, high in itertools.pairwise(bounds):
        store.write(_INSTANCE, _STREAM, data[low:high])
        more, offset, chunks = _follow(logs, offset, max_bytes)
        assert "�" not in more
        assert all(chunk.next_offset - chunk.offset <= max_bytes for chunk in chunks)
        seen += more
        assert text.startswith(seen)
    store.close(_INSTANCE, _STREAM)
    rest, offset, _ = _follow(logs, offset, max_bytes)
    assert seen + rest == text
    assert offset == len(data)


@settings(max_examples=examples(pr=200, nightly=2000))
@given(_TEXT, st.integers(LOGS_READ_MIN_BYTES, 64), st.integers(1, 80))
def test_a_window_more_bytes_follow_ends_after_its_last_line_end(
    text: str, max_bytes: int, at: int
) -> None:
    data = text.encode()
    store = _store()
    store.write(_INSTANCE, _STREAM, data)
    offset = min(at, len(data))
    chunk = _chunk(_read(_logs(store), LogOffset(offset=offset), max_bytes))
    window = data[offset : offset + max_bytes]
    if not chunk.eof and b"\n" in window:
        assert chunk.data.endswith("\n")
        assert chunk.next_offset == offset + window.rfind(b"\n") + 1


@settings(max_examples=examples(pr=200, nightly=2000))
@given(_TEXT, st.integers(1, 64))
def test_a_tail_starts_at_a_line_start_and_reaches_the_end(text: str, tail_bytes: int) -> None:
    data = text.encode()
    store = _store()
    store.write(_INSTANCE, _STREAM, data)
    store.close(_INSTANCE, _STREAM)
    chunk = _chunk(_read(_logs(store), LogTail(tail_bytes=tail_bytes), LOGS_READ_MAX_BYTES))
    assert chunk.eof
    assert chunk.next_offset == len(data)
    assert chunk.data == data[chunk.offset :].decode("utf-8", errors="replace")
    start = max(len(data) - tail_bytes, 0)
    window = data[start:]
    if start > 0 and b"\n" in window[:-1]:
        assert data[chunk.offset - 1 : chunk.offset] == b"\n"


def test_an_unfinished_character_at_the_end_of_an_open_stream_waits_for_its_last_byte() -> None:
    store = _store()
    euro = "€".encode()
    store.write(_INSTANCE, _STREAM, b"a" + euro[:2])
    logs = _logs(store)
    first = _chunk(_read(logs, LogOffset(offset=0), LOGS_READ_MAX_BYTES))
    assert (first.data, first.next_offset, first.eof) == ("a", 1, True)
    store.write(_INSTANCE, _STREAM, euro[2:])
    second = _chunk(_read(logs, LogOffset(offset=1), LOGS_READ_MAX_BYTES))
    assert (second.data, second.next_offset) == ("€", 4)


def test_reading_evicted_bytes_starts_at_the_oldest_byte_and_says_so() -> None:
    store = _store()
    store.write(_INSTANCE, _STREAM, b"one\ntwo\nthree\n")
    store.evict(_INSTANCE, _STREAM, 4)
    chunk = _chunk(_read(_logs(store), LogOffset(offset=0), LOGS_READ_MAX_BYTES))
    assert (chunk.offset, chunk.data, chunk.truncated_before) == (4, "two\nthree\n", True)


def test_unknown_instances_and_streams_are_typed_errors() -> None:
    logs = _logs(_store())
    listed = list_logs(LogsListRequest(request_id="l", deadline_ms=1, instance_id="f" * 12), logs)
    assert isinstance(listed, HelperError)
    assert listed.code is HelperErrorCode.UNKNOWN_INSTANCE
    for instance_id, stream_id, code in (
        ("f" * 12, _STREAM, HelperErrorCode.UNKNOWN_INSTANCE),
        (_INSTANCE, "missing", HelperErrorCode.UNKNOWN_LOG_STREAM),
    ):
        reply = _read(logs, LogOffset(offset=0), 64, stream_id=stream_id, instance_id=instance_id)
        assert isinstance(reply, HelperError)
        assert reply.code is code


def test_list_names_each_stream_with_its_size_and_state() -> None:
    store = _store()
    store.write(_INSTANCE, _STREAM, b"abc")
    store.open(_INSTANCE, "server", {"kind": "server_process"})
    store.close(_INSTANCE, "server")
    reply = list_logs(
        LogsListRequest(request_id="l", deadline_ms=1, instance_id=_INSTANCE), _logs(store)
    )
    assert isinstance(reply, LogsListReply)
    assert [(s.id, s.size, s.closed, s.command) for s in reply.streams] == [
        (_STREAM, 3, False, "docker build ."),
        ("server", 0, True, None),
    ]


# ----- the long poll, through the channel -----


def _hello() -> dict[str, Any]:
    return {
        "type": "hello",
        "request_id": "h",
        "deadline_ms": 5000,
        "client_versions": [1, 1],
        "features": ["logs"],
        "app_version": "test",
    }


def _read_frame(offset: int, *, wait_ms: int, deadline_ms: int) -> dict[str, Any]:
    return {
        "type": "logs.read",
        "request_id": "r",
        "deadline_ms": deadline_ms,
        "instance_id": _INSTANCE,
        "stream_id": _STREAM,
        "start": {"offset": offset},
        "max_bytes": LOGS_READ_MAX_BYTES,
        "wait_ms": wait_ms,
    }


@dataclass(frozen=True)
class _Followed:
    reply: dict[str, Any]
    waited: float


def _follow_on_channel(
    seed: int,
    request: dict[str, Any],
    meanwhile: Callable[[SimThreads, FakeLogReader], None],
    *,
    closed: bool = False,
) -> _Followed:
    """Serve one channel with an open stream holding ``ab``; send ``request``; run ``meanwhile``."""
    threads = SimThreads(schedule_seed=seed)
    network = SimNetwork(threads)
    store = FakeLogReader(threads, threads)
    store.open(_INSTANCE, _STREAM, _OWNER)
    store.write(_INSTANCE, _STREAM, b"ab")
    if closed:
        store.close(_INSTANCE, _STREAM)

    def main() -> _Followed:
        client = ChannelClient(threads)
        host = with_logs(fake_host(threads, network), store, threads)
        worker = serve_on(threads, client, ByteBuffer(), host)
        client.send(_hello())
        assert client.receive()["features"] == ["logs"]
        sent = threads.now()
        client.send(request)
        meanwhile(threads, store)
        reply = client.receive()
        waited = threads.now() - sent
        client.close()
        worker.join(HANG_GUARD_S)
        return _Followed(reply, waited)

    return threads.run(main)


@pytest.mark.parametrize("seed", range(3))
def test_a_waiting_read_answers_as_soon_as_bytes_arrive(seed: int) -> None:
    def write_later(threads: SimThreads, store: FakeLogReader) -> None:
        threads.sleep(3.0)
        store.write(_INSTANCE, _STREAM, b"c\n")

    followed = _follow_on_channel(
        seed, _read_frame(2, wait_ms=20_000, deadline_ms=30_000), write_later
    )
    assert followed.reply["type"] == "logs.read"
    assert followed.reply["chunk"]["data"] == "c\n"
    assert 3.0 <= followed.waited <= 3.0 + POLL_INTERVAL_SECONDS + 0.01


@pytest.mark.parametrize("seed", range(3))
def test_a_waiting_read_answers_before_its_deadline(seed: int) -> None:
    deadline_ms = 4000
    followed = _follow_on_channel(
        seed, _read_frame(2, wait_ms=30_000, deadline_ms=deadline_ms), lambda _t, _s: None
    )
    assert followed.reply["type"] == "logs.read"
    assert followed.reply["chunk"] == {
        "offset": 2,
        "data": "",
        "next_offset": 2,
        "eof": True,
        "closed": False,
        "truncated_before": False,
    }
    assert followed.waited < deadline_ms / 1000
    assert followed.waited >= (deadline_ms - REPLY_MARGIN_MS) / 1000


@pytest.mark.parametrize("seed", range(3))
def test_a_read_at_the_end_of_a_closed_stream_does_not_wait(seed: int) -> None:
    followed = _follow_on_channel(
        seed,
        _read_frame(2, wait_ms=30_000, deadline_ms=60_000),
        lambda _t, _s: None,
        closed=True,
    )
    assert followed.reply["chunk"]["closed"] is True
    assert followed.waited < POLL_INTERVAL_SECONDS


def test_a_host_without_a_log_store_does_not_offer_logs() -> None:
    threads = SimThreads(schedule_seed=0)
    network = SimNetwork(threads)

    def main() -> list[dict[str, Any]]:
        client = ChannelClient(threads)
        worker = serve_on(threads, client, ByteBuffer(), fake_host(threads, network))
        client.send(_hello())
        hello = client.receive()
        client.send(_read_frame(0, wait_ms=0, deadline_ms=1000))
        refused = client.receive()
        client.close()
        worker.join(HANG_GUARD_S)
        return [hello, refused]

    hello, refused = threads.run(main)
    assert hello["features"] == []
    assert (refused["type"], refused["code"]) == ("error", "feature_not_negotiated")


# ----- the LogReader contract -----


@dataclass(frozen=True)
class LogReaderUnderTest:
    """A reader, and the means to record what it then reads."""

    reader: LogReader
    open: Callable[[str, str], None]
    write: Callable[[str, str, bytes], None]
    close: Callable[[str, str], None]
    evict: Callable[[str, str, int], None]
    """Drop a stream's bytes before an offset, as the store's retention would."""


class LogReaderContract:
    """What the helper relies on from every ``LogReader``; a subclass names the implementation."""

    def subject(self) -> LogReaderUnderTest:
        raise NotImplementedError

    def test_reads_from_an_offset_at_most_max_bytes(self) -> None:
        subject = self.subject()
        subject.open(_INSTANCE, _STREAM)
        subject.write(_INSTANCE, _STREAM, b"0123456789")
        stored = subject.reader.read(_INSTANCE, _STREAM, LogOffset(offset=3), 4)
        assert (stored.offset, stored.data, stored.end) == (3, b"3456", 10)
        assert not stored.closed
        assert not stored.truncated_before

    def test_reads_past_the_end_return_no_bytes_at_that_offset(self) -> None:
        subject = self.subject()
        subject.open(_INSTANCE, _STREAM)
        subject.write(_INSTANCE, _STREAM, b"abc")
        stored = subject.reader.read(_INSTANCE, _STREAM, LogOffset(offset=9), 4)
        assert (stored.offset, stored.data, stored.end) == (9, b"", 3)

    def test_reads_the_tail_from_the_oldest_byte_held(self) -> None:
        subject = self.subject()
        subject.open(_INSTANCE, _STREAM)
        subject.write(_INSTANCE, _STREAM, b"0123456789")
        subject.close(_INSTANCE, _STREAM)
        tail = subject.reader.read(_INSTANCE, _STREAM, LogTail(tail_bytes=3), 64)
        assert (tail.offset, tail.data, tail.closed) == (7, b"789", True)
        subject.evict(_INSTANCE, _STREAM, 8)
        tail = subject.reader.read(_INSTANCE, _STREAM, LogTail(tail_bytes=3), 64)
        assert (tail.offset, tail.data, tail.truncated_before) == (8, b"89", False)

    def test_reads_of_evicted_bytes_start_at_the_oldest_held(self) -> None:
        subject = self.subject()
        subject.open(_INSTANCE, _STREAM)
        subject.write(_INSTANCE, _STREAM, b"0123456789")
        subject.evict(_INSTANCE, _STREAM, 6)
        stored = subject.reader.read(_INSTANCE, _STREAM, LogOffset(offset=2), 64)
        assert (stored.offset, stored.data, stored.truncated_before) == (6, b"6789", True)

    def test_lists_streams_oldest_first(self) -> None:
        subject = self.subject()
        subject.open(_INSTANCE, "first")
        subject.open(_INSTANCE, "second")
        subject.write(_INSTANCE, "second", b"xy")
        subject.close(_INSTANCE, "first")
        listed = subject.reader.streams(_INSTANCE)
        assert [(s.id, s.size, s.closed) for s in listed] == [
            ("first", 0, True),
            ("second", 2, False),
        ]
        json.loads(listed[0].model_dump_json())

    def test_unknown_instances_and_streams_raise(self) -> None:
        subject = self.subject()
        subject.open(_INSTANCE, _STREAM)
        with pytest.raises(UnknownInstanceError):
            subject.reader.streams("f" * 12)
        with pytest.raises(UnknownInstanceError):
            subject.reader.read("f" * 12, _STREAM, LogOffset(offset=0), 4)
        with pytest.raises(UnknownLogStreamError):
            subject.reader.read(_INSTANCE, "missing", LogOffset(offset=0), 4)


class TestFakeLogReader(LogReaderContract):
    def subject(self) -> LogReaderUnderTest:
        store = FakeLogReader(ManualClock(), SimThreads(schedule_seed=0))
        return LogReaderUnderTest(
            reader=store,
            open=lambda instance, stream: store.open(instance, stream, _OWNER),
            write=store.write,
            close=store.close,
            evict=store.evict,
        )
