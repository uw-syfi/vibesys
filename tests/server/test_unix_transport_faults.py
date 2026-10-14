"""The Unix JSONL transport under client faults at every message boundary.

The server runs on ``SimNetwork`` and ``SimThreads``, so a schedule is a seed
and every wait is virtual. For every request kind, a client sends its request
whole, in fragments, twice, or cut short; pauses; reads some or all of the
answer; and hangs up. Whatever it does, the server must not fail a worker, must
leave no subscription counted, and must answer the next client.
"""

from __future__ import annotations

import json
import typing
from contextlib import suppress
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.support import build_server_parts

from server.api.protocol import (
    EventBatchMessage,
    ProtocolRequest,
    Response,
    SubscribedMessage,
)
from server.transport.subscriptions import SubscriptionTracker
from server.transport.unix_jsonl import MAX_REQUEST_LINE_BYTES as MAX
from server.transport.unix_jsonl import UnixJsonlServer
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vs_sim.api import Connection

# The fields a request kind cannot default, so every kind has one valid frame.
_REQUIRED: dict[str, dict[str, Any]] = {
    "command.steer": {"text": "focus on the cache"},
    "query.chat": {"text": "what changed?"},
    "query.design_patch": {"base": "a", "head": "b", "path": "x.py"},
}
# Every member of ``ProtocolRequest``, so a new kind cannot skip this harness.
_KINDS = sorted(
    member.model_fields["type"].default
    for member in typing.get_args(typing.get_args(ProtocolRequest)[0])
)
# What a subscriber reads before its stream goes quiet: the acknowledgment and
# the bootstrap batch.
_SUBSCRIBE_LINES = 2
# The size of one server read, so lines straddle reads at every offset.
_RECV_CHUNK = 65536


def _frame(kind: str) -> bytes:
    body = {"type": kind, "request_id": f"req-{kind}", **_REQUIRED.get(kind, {})}
    return json.dumps(body).encode() + b"\n"


@dataclass(frozen=True)
class _Fault:
    """What one client does to its exchange, boundary by boundary."""

    kind: str
    sent: int
    """Bytes of the request sent; fewer than the frame is a truncated request."""
    cuts: tuple[int, ...]
    """Where the sent bytes are split across writes."""
    duplicate: bool
    """Send the whole frame a second time."""
    pause: float
    """Virtual seconds between sending and reading."""
    read: int
    """Answer lines read before hanging up."""

    @property
    def whole(self) -> bool:
        return self.sent == len(_frame(self.kind))

    @property
    def answers(self) -> int:
        """Lines the server owes this client before it can only wait."""
        if not self.whole:
            return 0
        if self.kind == "subscribe":
            return _SUBSCRIBE_LINES
        return 2 if self.duplicate else 1


@st.composite
def _faults(draw: st.DrawFn) -> _Fault:
    kind = draw(st.sampled_from(_KINDS))
    size = len(_frame(kind))
    sent = draw(st.sampled_from([0, 1, size - 1, size]) | st.integers(0, size))
    cuts = tuple(sorted(draw(st.sets(st.integers(1, max(sent - 1, 1)), max_size=3))))
    duplicate = sent == size and draw(st.booleans())
    pause = draw(st.sampled_from([0.0, 0.05, 1.0, 30.0]))
    fault = _Fault(kind, sent, cuts, duplicate, pause, 0)
    return _Fault(kind, sent, cuts, duplicate, pause, draw(st.integers(0, fault.answers)))


def _read_line(connection: Connection) -> bytes:
    line = b""
    while not line.endswith(b"\n"):
        chunk = connection.recv(1, HANG_GUARD_S)
        assert chunk, f"the server hung up mid-line after {line!r}"
        line += chunk
    return line


def _send(connection: Connection, data: bytes, cuts: tuple[int, ...]) -> None:
    bounds = [0, *(cut for cut in cuts if 0 < cut < len(data)), len(data)]
    for start, end in pairwise(bounds):
        if end > start:
            connection.send(data[start:end])


def _exchange(connection: Connection, fault: _Fault, threads: SimThreads) -> list[bytes]:
    frame = _frame(fault.kind)
    _send(connection, frame[: fault.sent], fault.cuts)
    if fault.duplicate:
        connection.send(frame)
    threads.sleep(fault.pause)
    lines = [_read_line(connection) for _ in range(fault.read)]
    connection.close()
    return lines


def _check_answers(fault: _Fault, lines: list[bytes]) -> None:
    if fault.kind == "subscribe":
        replies = [SubscribedMessage.model_validate_json(line) for line in lines[:1]]
        for line in lines[1:]:
            EventBatchMessage.model_validate_json(line)
    else:
        replies = [Response.model_validate_json(line) for line in lines]
    assert all(reply.request_id == f"req-{fault.kind}" for reply in replies), replies


# About 45 ms an example: three servers' worth of simulated threads per draw.
# Depth lives in the nightly run.
@settings(deadline=None, max_examples=examples(pr=10, nightly=600))
@given(faults=st.lists(_faults(), min_size=1, max_size=3), seed=st.integers(0, 2**32))
def test_a_client_fault_at_any_boundary_costs_only_that_client(
    tmp_path_factory: pytest.TempPathFactory, faults: list[_Fault], seed: int
) -> None:
    """Concurrent faulty clients, then one healthy one, on one server.

    - No server worker fails: a peer that is gone, at any boundary, is a
      normal end of its session.
    - Every answer a client did read is a well-formed reply to its request.
    - Every stream ends once its client hangs up, so no subscription stays
      counted against the server's lifetime.
    - The next client is answered as if nothing had happened.
    """
    socket_path: Path = tmp_path_factory.mktemp("sock") / "control.sock"
    threads = SimThreads(schedule_seed=seed)

    def main() -> None:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        tracker = SubscriptionTracker(threads=threads)
        with UnixJsonlServer(socket_path, parts.api, tracker, threads, network):
            done = []
            for index, fault in enumerate(faults):
                client = network.connect(str(socket_path))
                threads.spawn(
                    lambda c=client, f=fault: done.append((f, _exchange(c, f, threads))),
                    name=f"client-{index}",
                    daemon=False,
                )
            settled = threads.event()

            def drained() -> None:
                while len(done) < len(faults):
                    threads.sleep(0.01)
                tracker.wait_for_none_active(settle_seconds=0)
                settled.set()

            threads.spawn(drained, name="drained")
            assert settled.wait(HANG_GUARD_S), "a stream outlived its client"
            for fault, lines in done:
                _check_answers(fault, lines)

            healthy = network.connect(str(socket_path))
            healthy.send(_frame("query.snapshot"))
            reply = Response.model_validate_json(_read_line(healthy))
            assert reply.ok, reply
            healthy.close()

    threads.run(main)


def test_a_client_that_hangs_up_before_its_answer_ends_only_its_own_session(
    tmp_path: Path,
) -> None:
    """The answer to a request whose client already hung up is dropped quietly.

    Writing it raises ``BrokenPipeError``. On ``socketserver`` that escaped the
    handler and printed a traceback to stderr for an ordinary hangup; here it is
    the end of that session and nothing else.
    """
    threads = SimThreads(schedule_seed=0)
    socket_path = tmp_path / "control.sock"

    def main() -> None:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        with UnixJsonlServer(socket_path, parts.api, threads=threads, network=network):
            gone = network.connect(str(socket_path))
            gone.send(_frame("query.snapshot"))
            gone.close()
            threads.sleep(1.0)
            again = network.connect(str(socket_path))
            again.send(_frame("query.snapshot"))
            assert Response.model_validate_json(_read_line(again)).ok
            again.close()

    threads.run(main)


def _read_until_hangup(connection: Connection) -> list[bytes]:
    data = b""
    while chunk := connection.recv(4096, HANG_GUARD_S):
        data += chunk
    connection.close()
    return data.splitlines(keepends=True)


@settings(deadline=None, max_examples=examples(pr=10, nightly=300))
@given(
    faults=st.lists(_faults(), min_size=1, max_size=3),
    outputs=st.integers(0, 30),
    close_after=st.sampled_from([0.0, 0.05, 0.5, 5.0]),
    seed=st.integers(0, 2**32),
)
def test_closing_the_server_hangs_up_on_every_client_however_far_it_got(
    tmp_path_factory: pytest.TempPathFactory,
    faults: list[_Fault],
    outputs: int,
    close_after: float,
    seed: int,
) -> None:
    """``close`` ends every session, mid-request, mid-answer or mid-stream.

    The clients never hang up themselves and the run keeps publishing output,
    so only the server's close can end their sessions. Each client then reads
    its connection to the end, and every whole line it got is a reply to it.
    """
    socket_path: Path = tmp_path_factory.mktemp("sock") / "control.sock"
    threads = SimThreads(schedule_seed=seed)

    def main() -> None:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        tracker = SubscriptionTracker(threads=threads)
        server = UnixJsonlServer(socket_path, parts.api, tracker, threads, network)
        server.start()
        done: list[tuple[_Fault, list[bytes]]] = []
        for index, fault in enumerate(faults):
            client = network.connect(str(socket_path))

            def run(c: Connection = client, f: _Fault = fault) -> None:
                frame = _frame(f.kind)
                # The server may close first, which a write sees as a broken pipe.
                with suppress(BrokenPipeError):
                    _send(c, frame[: f.sent], f.cuts)
                    if f.duplicate:
                        c.send(frame)
                done.append((f, _read_until_hangup(c)))

            threads.spawn(run, name=f"client-{index}", daemon=False)

        def publish() -> None:
            for index in range(outputs):
                parts.journal.publish_output("stdout", f"line {index}")
                threads.sleep(0.01)

        threads.spawn(publish, name="publisher", daemon=False)
        threads.sleep(close_after)
        server.close()
        settled = threads.event()

        def drained() -> None:
            tracker.wait_for_none_active(settle_seconds=0)
            settled.set()

        threads.spawn(drained, name="drained")
        assert settled.wait(HANG_GUARD_S), "a stream outlived the server's close"
        for fault, lines in done:
            whole = [line for line in lines if line.endswith(b"\n")]
            _check_answers(fault, whole[: 1 if fault.kind == "subscribe" else len(whole)])

    threads.run(main)


def test_a_subscriber_that_wrote_after_subscribing_is_seen_hanging_up_on_an_idle_run(
    tmp_path: Path,
) -> None:
    """Bytes a subscriber sends after ``subscribe`` cannot hide its hangup.

    A subscription reads no further requests, and a peer that closed with
    bytes still unread is not yet at end of stream. With nothing to publish
    the stream never writes, so no failed write ends it either: before this
    was handled, the stream stayed counted for ever and blocked the server's
    teardown, which waits for every subscription to end.
    """
    threads = SimThreads(schedule_seed=0)
    socket_path = tmp_path / "control.sock"

    def main() -> None:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        tracker = SubscriptionTracker(threads=threads)
        with UnixJsonlServer(socket_path, parts.api, tracker, threads, network):
            client = network.connect(str(socket_path))
            client.send(_frame("subscribe"))
            assert tracker.wait_for_subscriber(HANG_GUARD_S)
            client.send(_frame("query.snapshot"))
            client.close()
            settled = threads.event()

            def drained() -> None:
                tracker.wait_for_none_active(settle_seconds=0)
                settled.set()

            threads.spawn(drained, name="drained")
            assert settled.wait(HANG_GUARD_S), "the stream outlived its client"

    threads.run(main)


def test_a_frame_nested_past_the_recursion_limit_is_rejected_and_the_session_goes_on(
    tmp_path: Path,
) -> None:
    """The rejection is an answer like any other; the client's next request is served.

    Decoding once recursed per nesting level, so a frame of ``[`` deeper than
    Python's recursion limit raised ``RecursionError`` out of the session and
    ended it with no answer.
    """
    threads = SimThreads(schedule_seed=0)
    socket_path = tmp_path / "control.sock"

    def main() -> None:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        with UnixJsonlServer(socket_path, parts.api, threads=threads, network=network):
            client = network.connect(str(socket_path))
            client.send(b"[" * 100_000 + b"\n" + _frame("query.snapshot"))
            rejection = Response.model_validate_json(_read_line(client))
            assert not rejection.ok
            assert rejection.request_id == "unknown"
            assert Response.model_validate_json(_read_line(client)).ok
            client.close()

    threads.run(main)


@settings(deadline=None, max_examples=examples(pr=6, nightly=200))
@given(
    excess=st.sampled_from([0, 1, _RECV_CHUNK - 1, _RECV_CHUNK]) | st.integers(0, 3 * MAX),
    cuts=st.sets(st.integers(1, 4 * MAX), max_size=4),
    seed=st.integers(0, 2**32),
)
def test_a_request_line_past_the_bound_is_rejected_and_the_session_goes_on(
    tmp_path_factory: pytest.TempPathFactory, excess: int, cuts: set[int], seed: int
) -> None:
    """A line of ``MAX_REQUEST_LINE_BYTES`` or more draws one typed rejection.

    The session once buffered a line until its newline, however long, so one
    client could grow the server's memory without bound. Now the line is
    answered ``request_too_large`` once, the rest of it is dropped as it
    arrives, and the request after it is served.
    """
    socket_path: Path = tmp_path_factory.mktemp("sock") / "control.sock"
    threads = SimThreads(schedule_seed=seed)
    oversized = b"x" * (MAX + excess) + b"\n"

    def main() -> None:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        with UnixJsonlServer(socket_path, parts.api, threads=threads, network=network):
            client = network.connect(str(socket_path))
            _send(client, oversized + _frame("query.snapshot"), tuple(sorted(cuts)))
            rejection = Response.model_validate_json(_read_line(client))
            assert not rejection.ok
            assert rejection.diagnostic is not None
            assert rejection.diagnostic.code == "request_too_large"
            reply = Response.model_validate_json(_read_line(client))
            assert reply.ok, reply
            assert reply.request_id == "req-query.snapshot"
            client.close()

    threads.run(main)
