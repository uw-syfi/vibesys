"""The host helper's ``logs`` feature: chunk cutting, tails, the long poll, and the gate.

Reads run through :func:`read_log` and :func:`list_logs` over ``FakeLogReader``, which
``test_log_reader_contract.py`` holds to the ``LogReader`` contract with the real
``StoreLogReader``. The long poll runs through the channel on
``SimThreads``, so its waits are virtual time.
"""

from __future__ import annotations

import itertools
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
    record,
    serve_on,
    with_logs,
)

from server.host_api import (
    LOGS_READ_MAX_BYTES,
    LOGS_READ_MIN_BYTES,
    HelperError,
    HelperErrorCode,
    InspectionState,
    InstanceInspection,
    InstanceInspectionMetadata,
    LogChunk,
    LogOffset,
    LogPipe,
    LogsListReply,
    LogsListRequest,
    LogsReadReply,
    LogsReadRequest,
    LogStreamOutcome,
    LogTail,
)
from server.host_helper import MAX_EXECUTING_REQUESTS
from server.host_helper_logs import (
    POLL_INTERVAL_SECONDS,
    REPLY_MARGIN_MS,
    HelperLogs,
    RunLocation,
    list_logs,
    read_log,
    run_location,
)
from vs_sim.api.testing import HANG_GUARD_S, ManualClock, SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable

_INSTANCE = "0123456789ab"
_STREAM = "000001"
_OTHER = "000002"
_MISSING = "000099"
_OWNER: dict[str, Any] = {"kind": "startup_step", "step": "image_build"}

# Text with one-, two-, three- and four-byte characters and line ends.
_TEXT = st.text(alphabet=st.sampled_from(["a", "\n", "é", "€", "😀", " "]), max_size=200)
_READ_SIZES = st.integers(LOGS_READ_MIN_BYTES, 64)


def _store() -> FakeLogReader:
    store = FakeLogReader(SimThreads(schedule_seed=0))
    store.open(_INSTANCE, _STREAM, _OWNER, "docker build .")
    return store


@dataclass(frozen=True)
class _NeverWithdrawn:
    """A ``Withdrawal`` nobody sets: each wait lets its whole timeout pass on ``clock``."""

    clock: ManualClock

    def wait(self, timeout: float) -> bool:
        self.clock.advance(timeout)
        return False


def _logs(store: FakeLogReader) -> HelperLogs:
    return HelperLogs(store, ManualClock())


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
    assert isinstance(logs.clock, ManualClock)
    return read_log(request, logs, _NeverWithdrawn(logs.clock))


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
@given(
    st.lists(st.tuples(st.binary(max_size=40), st.sampled_from(list(LogPipe))), max_size=8),
    _READ_SIZES,
)
def test_reading_a_closed_stream_in_chunks_decodes_like_reading_it_whole(
    writes: list[tuple[bytes, LogPipe]], max_bytes: int
) -> None:
    store = _store()
    for data, pipe in writes:
        store.write(_INSTANCE, _STREAM, data, pipe)
    store.close(_INSTANCE, _STREAM)
    text, end, chunks = _follow(_logs(store), 0, max_bytes)
    whole = b"".join(data for data, _ in writes)
    assert text == whole.decode("utf-8", errors="replace")
    assert end == len(whole)
    labels = [pipe for data, pipe in writes for _ in data]
    seen = [span.pipe for chunk in chunks for span in chunk.pipes for _ in range(span.length)]
    assert seen == labels
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
        (_INSTANCE, _MISSING, HelperErrorCode.UNKNOWN_LOG_STREAM),
    ):
        reply = _read(logs, LogOffset(offset=0), 64, stream_id=stream_id, instance_id=instance_id)
        assert isinstance(reply, HelperError)
        assert reply.code is code


def test_list_names_each_stream_with_its_size_and_state() -> None:
    store = _store()
    store.write(_INSTANCE, _STREAM, b"abc")
    store.open(_INSTANCE, _OTHER, {"kind": "server_process"}, "vibesys")
    store.close(_INSTANCE, _OTHER, LogStreamOutcome.FAILED)
    reply = list_logs(
        LogsListRequest(request_id="l", deadline_ms=1, instance_id=_INSTANCE), _logs(store)
    )
    assert isinstance(reply, LogsListReply)
    assert [(s.id, s.size, s.outcome, s.command) for s in reply.streams] == [
        (_STREAM, 3, None, "docker build ."),
        (_OTHER, 0, LogStreamOutcome.FAILED, "vibesys"),
    ]


@settings(max_examples=examples(pr=6, nightly=50), deadline=None)
@given(owners=st.lists(st.sampled_from([None, "flow:w0/#1", "op:w0/worker.1"]), max_size=4))
def test_list_by_span_keeps_exactly_the_streams_whose_owner_names_it(
    owners: list[str | None],
) -> None:
    store = _store()
    for number, span_id in enumerate(owners):
        named = {} if span_id is None else {"span_id": span_id}
        store.open(_INSTANCE, f"{900000 + number}", {"kind": "run_narration", **named}, "vibesys")
    request = LogsListRequest(
        request_id="l", deadline_ms=1, instance_id=_INSTANCE, span_id="flow:w0/#1"
    )
    reply = list_logs(request, _logs(store))
    assert isinstance(reply, LogsListReply)
    named = [f"{900000 + n}" for n, span_id in enumerate(owners) if span_id == "flow:w0/#1"]
    assert [s.id for s in reply.streams if s.id != _STREAM] == named


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


def _read_frame(
    offset: int,
    *,
    wait_ms: int,
    deadline_ms: int,
    request_id: str = "r",
    stream_id: str = _STREAM,
) -> dict[str, Any]:
    return {
        "type": "logs.read",
        "request_id": request_id,
        "deadline_ms": deadline_ms,
        "instance_id": _INSTANCE,
        "stream_id": stream_id,
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
    store = FakeLogReader(threads)
    store.open(_INSTANCE, _STREAM, _OWNER, "docker build .")
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
        "pipes": [],
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


def test_a_waiting_read_opens_the_instances_logs_once() -> None:
    store: list[FakeLogReader] = []

    def write_later(threads: SimThreads, written: FakeLogReader) -> None:
        store.append(written)
        threads.sleep(10 * POLL_INTERVAL_SECONDS)
        written.write(_INSTANCE, _STREAM, b"c\n")

    followed = _follow_on_channel(
        0, _read_frame(2, wait_ms=20_000, deadline_ms=30_000), write_later
    )
    assert followed.reply["chunk"]["data"] == "c\n"
    assert store[0].opened == [_INSTANCE]


# ----- withdrawing a waiting read (logs.cancel) -----

_FOLLOW_WAIT_MS = 25_000
"""The wait a follower asks for, as the desktop app's follower does."""


def _cancel_frame(request_id: str, read_id: str) -> dict[str, Any]:
    return {
        "type": "logs.cancel",
        "request_id": request_id,
        "deadline_ms": 5000,
        "read_id": read_id,
    }


def _on_cancel_channel[T](
    seed: int, exchange: Callable[[SimThreads, ChannelClient], T], *, features: list[str]
) -> T:
    """Serve one channel over two open streams at their end; run ``exchange`` after ``hello``."""
    threads = SimThreads(schedule_seed=seed)
    network = SimNetwork(threads)
    store = FakeLogReader(threads)
    for stream_id in (_STREAM, _OTHER):
        store.open(_INSTANCE, stream_id, _OWNER, "docker build .")
        store.write(_INSTANCE, stream_id, b"ab")

    def main() -> T:
        client = ChannelClient(threads)
        host = with_logs(fake_host(threads, network), store, threads)
        worker = serve_on(threads, client, ByteBuffer(), host)
        client.send({**_hello(), "features": features})
        assert client.receive()["features"] == features
        result = exchange(threads, client)
        client.close()
        worker.join(HANG_GUARD_S)
        return result

    return threads.run(main)


@pytest.mark.parametrize("seed", range(3))
def test_a_withdrawn_read_answers_at_once_and_frees_its_worker(seed: int) -> None:
    def exchange(threads: SimThreads, client: ChannelClient) -> list[dict[str, Any]]:
        for index in range(MAX_EXECUTING_REQUESTS):
            client.send(
                _read_frame(2, wait_ms=_FOLLOW_WAIT_MS, deadline_ms=30_000, request_id=f"r{index}")
            )
        client.send(_read_frame(2, wait_ms=0, deadline_ms=30_000, request_id="busy"))
        refused = client.receive()
        sent = threads.now()
        client.send(_cancel_frame("c", "r0"))
        answered = [client.receive(), client.receive()]
        assert threads.now() == sent
        client.send(_read_frame(2, wait_ms=0, deadline_ms=30_000, request_id="next"))
        return [refused, *answered, client.receive()]

    refused, read, cancel, admitted = _on_cancel_channel(
        seed, exchange, features=["logs", "logs_cancel"]
    )
    assert (refused["request_id"], refused["code"]) == ("busy", "channel_busy")
    assert (read["type"], read["request_id"], read["chunk"]["data"]) == ("logs.read", "r0", "")
    assert cancel == {"type": "logs.cancel", "request_id": "c", "read_id": "r0"}
    assert (admitted["type"], admitted["request_id"]) == ("logs.read", "next")


@pytest.mark.parametrize("seed", range(3))
def test_flipping_streams_past_the_worker_limit_is_never_busy(seed: int) -> None:
    """A follower that switches streams withdraws the read it leaves; no flip is refused.

    Each flip sends the new stream's read once the last withdrawal was answered, which
    says the withdrawn read's worker returned; no virtual time passes for any of it.
    """
    flips = 2 * MAX_EXECUTING_REQUESTS + 1

    def exchange(threads: SimThreads, client: ChannelClient) -> tuple[list[dict[str, Any]], float]:
        started = threads.now()
        replies = []
        for flip in range(flips):
            stream_id = (_STREAM, _OTHER)[flip % 2]
            client.send(
                _read_frame(
                    2,
                    wait_ms=_FOLLOW_WAIT_MS,
                    deadline_ms=30_000,
                    request_id=f"f{flip}",
                    stream_id=stream_id,
                )
            )
            client.send(_cancel_frame(f"c{flip}", f"f{flip}"))
            replies += [client.receive(), client.receive()]
        return replies, threads.now() - started

    replies, took = _on_cancel_channel(seed, exchange, features=["logs", "logs_cancel"])
    assert [(r["type"], r["request_id"]) for r in replies] == [
        pair
        for flip in range(flips)
        for pair in (("logs.read", f"f{flip}"), ("logs.cancel", f"c{flip}"))
    ]
    assert took == 0


@pytest.mark.parametrize("seed", range(3))
def test_withdrawing_no_waiting_read_is_answered_at_once(seed: int) -> None:
    def exchange(_threads: SimThreads, client: ChannelClient) -> list[dict[str, Any]]:
        client.send(_cancel_frame("c0", "never-sent"))
        first = client.receive()
        client.send(_read_frame(0, wait_ms=0, deadline_ms=30_000, request_id="done"))
        read = client.receive()
        client.send(_cancel_frame("c1", "done"))
        return [first, read, client.receive()]

    first, read, late = _on_cancel_channel(seed, exchange, features=["logs", "logs_cancel"])
    assert first == {"type": "logs.cancel", "request_id": "c0", "read_id": "never-sent"}
    assert read["type"] == "logs.read"
    assert late == {"type": "logs.cancel", "request_id": "c1", "read_id": "done"}


def test_withdrawal_needs_its_feature() -> None:
    def exchange(_threads: SimThreads, client: ChannelClient) -> dict[str, Any]:
        client.send(_cancel_frame("c", "r"))
        return client.receive()

    refused = _on_cancel_channel(0, exchange, features=["logs"])
    assert (refused["type"], refused["code"]) == ("error", "feature_not_negotiated")


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


# ----- finding an instance's run -----


def _inspection(*, live: bool, retained: bool) -> InstanceInspection:
    metadata = InstanceInspectionMetadata(
        id=_INSTANCE,
        project_root="/retained",
        run_id="run-2",
        pid=4242,
        started_at=1.0,
        hostname="node",
        vibesys_version="0+test",
        control_socket_path="/run/sock",
        server_log_path="/run/server.log",
    )
    return InstanceInspection(
        id=_INSTANCE,
        state=InspectionState.LIVE if live else InspectionState.ENDED,
        record=record(_INSTANCE, run_id="run-1", project_root="/live") if live else None,
        metadata=metadata if retained else None,
        control_socket_path="/run/sock",
        server_log_path="/run/server.log",
    )


@pytest.mark.parametrize(
    ("live", "retained", "expected"),
    [
        (True, True, RunLocation("/live", "run-1")),
        (True, False, RunLocation("/live", "run-1")),
        (False, True, RunLocation("/retained", "run-2")),
        (False, False, None),
    ],
)
def test_an_instances_run_comes_from_its_live_record_or_its_retained_metadata(
    *, live: bool, retained: bool, expected: RunLocation | None
) -> None:
    assert run_location(_inspection(live=live, retained=retained)) == expected
