"""The Unix JSONL transport under client faults at every message boundary.

The server runs on ``SimNetwork`` and ``SimThreads``, so a schedule is a seed
and every wait is virtual. For every request kind, a client sends its request
whole, in fragments, twice, or cut short; pauses; reads some or all of the
answer; and hangs up. Whatever it does, the server must not fail a worker, must
leave no subscription counted, and must answer the next client.
"""

from __future__ import annotations

import errno
import json
import typing
from contextlib import suppress
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter
from tests.server.support import REQUEST_TIME, build_server_parts

from server.api.protocol import (
    MAX_SERVER_MESSAGE_BYTES,
    ActiveExecutions,
    ActiveExecutionsChunkMessage,
    ChatQuery,
    EventBatchMessage,
    ProtocolRequest,
    Response,
    ServerMessage,
    SubscribedMessage,
)
from server.chat.manager import ChatAnswer
from server.transport.subscriptions import SubscriptionTracker
from server.transport.unix_jsonl import MAX_REQUEST_LINE_BYTES as MAX
from server.transport.unix_jsonl import UnixJsonlServer
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vs_sim.api import Connection, Listener

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
    body = {
        "type": kind,
        "request_id": f"req-{kind}",
        "timestamp": "2026-01-01T00:00:00Z",
        **_REQUIRED.get(kind, {}),
    }
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


def _read_buffered_line(connection: Connection, pending: bytearray) -> bytes:
    """Read one large line without making its byte count its scheduling count."""
    while b"\n" not in pending:
        chunk = connection.recv(_RECV_CHUNK, HANG_GUARD_S)
        assert chunk, "the server hung up mid-line"
        pending.extend(chunk)
    end = pending.index(b"\n") + 1
    line = bytes(pending[:end])
    del pending[:end]
    return line


def test_unix_subscription_uses_the_same_bounded_active_checkpoint_frames(
    tmp_path: Path,
) -> None:
    """The desktop's JSONL path has the WebSocket checkpoint semantics."""
    threads = SimThreads(schedule_seed=7)

    def main() -> None:
        parts = build_server_parts(None, threads=threads)
        assignment = "x" * (MAX_SERVER_MESSAGE_BYTES + 64 * 1024)
        execution = parts.start_execution("implementer", "round-1", assignment)
        network = SimNetwork(threads)
        socket_path = tmp_path / "control.sock"
        with UnixJsonlServer(
            socket_path,
            parts.api,
            subscriptions=None,
            threads=threads,
            network=network,
        ):
            client = network.connect(str(socket_path))
            client.send(_frame("subscribe"))
            pending = bytearray()
            SubscribedMessage.model_validate_json(_read_buffered_line(client, pending))
            batch_line = _read_buffered_line(client, pending)
            assert len(batch_line.removesuffix(b"\n")) <= MAX_SERVER_MESSAGE_BYTES
            batch = TypeAdapter(ServerMessage).validate_json(batch_line)
            assert isinstance(batch, EventBatchMessage)
            assert batch.active_executions is None
            chunks: list[ActiveExecutionsChunkMessage] = []
            while not chunks or chunks[-1].chunk_index + 1 != chunks[-1].chunk_count:
                line = _read_buffered_line(client, pending)
                assert len(line.removesuffix(b"\n")) <= MAX_SERVER_MESSAGE_BYTES
                message = TypeAdapter(ServerMessage).validate_json(line)
                assert isinstance(message, ActiveExecutionsChunkMessage)
                chunks.append(message)
            active = TypeAdapter(ActiveExecutions).validate_json(
                "".join(chunk.payload for chunk in chunks)
            )
            assert [(item.execution_id, item.assignment) for item in active] == [
                (execution.execution_id, assignment)
            ]
            client.close()

    threads.run(main)


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
        with UnixJsonlServer(
            socket_path, parts.api, threads=threads, network=network, subscriptions=None
        ):
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
        clients_done = threads.event()
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
                if len(done) == len(faults):
                    clients_done.set()

            threads.spawn(run, name=f"client-{index}", daemon=False)

        def publish() -> None:
            for index in range(outputs):
                parts.journal.publish_output("stdout", f"line {index}")
                threads.sleep(0.01)

        threads.spawn(publish, name="publisher", daemon=False)
        threads.sleep(close_after)
        server.close()
        assert clients_done.wait(HANG_GUARD_S), "a client did not observe the server's close"
        tracker.wait_for_none_active(settle_seconds=0)
        assert len(done) == len(faults)
        for fault, lines in done:
            whole = [line for line in lines if line.endswith(b"\n")]
            _check_answers(fault, whole[: 1 if fault.kind == "subscribe" else len(whole)])

    threads.run(main)


def test_close_drains_a_started_request_before_returning(tmp_path: Path) -> None:
    """Closing a connection does not abandon synchronous API work already in flight."""
    threads = SimThreads(schedule_seed=0)
    socket_path = tmp_path / "control.sock"

    def main() -> None:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        entered = threads.event()
        release = threads.event()
        finished = threads.event()

        def answer(_question: str) -> ChatAnswer:
            entered.set()
            assert release.wait(HANG_GUARD_S), "the test did not release the chat request"
            # Keep the handler unfinished for one virtual instant after release.
            # A close that merely drops the connection remains runnable and
            # therefore returns before simulated time can advance to this work.
            threads.sleep(1)
            finished.set()
            return ChatAnswer(text="finished", invocation_id="chat-1")

        parts.chat.install_default_handler(answer)
        server = UnixJsonlServer(
            socket_path, parts.api, threads=threads, network=network, subscriptions=None
        )
        server.start()
        client = network.connect(str(socket_path))
        client.send(
            ChatQuery(timestamp=REQUEST_TIME, text="finish before close").model_dump_json().encode()
            + b"\n"
        )
        assert entered.wait(HANG_GUARD_S), "the chat request did not start"
        closed = threads.event()

        def close_server() -> None:
            server.close()
            assert finished.is_set(), "close abandoned an admitted request worker"
            closed.set()

        threads.spawn(close_server, name="closer", daemon=False)
        assert client.recv(1, HANG_GUARD_S) == b""
        release.set()
        assert closed.wait(HANG_GUARD_S), "the server did not finish closing"
        client.close()
        server.close()  # idempotent after every owned worker has ended
        parts.close()

    threads.run(main)


def test_close_called_by_requests_does_not_cross_join_their_workers(tmp_path: Path) -> None:
    """Concurrent reentrant closes initiate shutdown without joining each other."""
    threads = SimThreads(schedule_seed=0)
    socket_path = tmp_path / "control.sock"

    def main() -> None:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        server = UnixJsonlServer(
            socket_path, parts.api, threads=threads, network=network, subscriptions=None
        )
        arrived: list[str] = []
        all_entered = threads.event()
        returns: list[str] = []
        returned = threads.event()

        def answer(question: str) -> ChatAnswer:
            arrived.append(question)
            if len(arrived) == 2:
                all_entered.set()
            assert all_entered.wait(HANG_GUARD_S), "both close requests did not enter"
            server.close()
            returns.append(question)
            if len(returns) == 2:
                returned.set()
            return ChatAnswer(text="closed", invocation_id=question)

        parts.chat.install_default_handler(answer)
        server.start()
        clients = [network.connect(str(socket_path)) for _ in range(2)]
        for index, client in enumerate(clients):
            client.send(
                ChatQuery(timestamp=REQUEST_TIME, text=f"close {index}").model_dump_json().encode()
                + b"\n"
            )

        assert returned.wait(HANG_GUARD_S), "reentrant closes joined request workers"
        for client in clients:
            assert client.recv(1, HANG_GUARD_S) == b""
            client.close()
        server.close()
        server.close()
        parts.close()

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
        with UnixJsonlServer(
            socket_path, parts.api, threads=threads, network=network, subscriptions=None
        ):
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
        with UnixJsonlServer(
            socket_path, parts.api, threads=threads, network=network, subscriptions=None
        ):
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


class _AcceptFaultListener:
    """Wrap a listener, raising ``error`` instead of delegating on chosen ``accept()`` calls.

    Models a transient ``accept()`` failure (``EMFILE``/``ENFILE`` under
    file-descriptor pressure): the wrapped listener is never closed, so once
    the faulty calls are behind it, ``accept()`` works exactly as it did.
    """

    def __init__(self, inner: Listener, fail_at: frozenset[int], error: OSError) -> None:
        self._inner = inner
        self._fail_at = fail_at
        self._error = error
        self.calls = 0

    def accept(self, timeout: float | None = None) -> Connection:
        self.calls += 1
        if self.calls in self._fail_at:
            raise self._error
        return self._inner.accept(timeout)

    def close(self) -> None:
        self._inner.close()


class _RelabeledErrorListener:
    """Wrap a listener, reporting any ``accept()`` ``OSError`` it raises as ``error`` instead.

    A deliberate close must end the accept loop whatever errno the platform
    happens to surface for it; this lets a test drive a real close through
    ``Listener.close()`` while making that errno look exactly like a transient
    failure (``EMFILE``), to prove the loop's branch comes from ``_closing``
    and not from guessing at the exception.
    """

    def __init__(self, inner: Listener, error: OSError) -> None:
        self._inner = inner
        self._error = error
        self.calls = 0

    def accept(self, timeout: float | None = None) -> Connection:
        self.calls += 1
        if self.calls > 1:
            message = "the accept loop retried after its listener was deliberately closed"
            raise AssertionError(message)
        try:
            return self._inner.accept(timeout)
        except OSError:
            raise self._error from None

    def close(self) -> None:
        self._inner.close()


class _DeferredCloseListener:
    """A listener whose close is observed only by a bounded ``accept``.

    Some Unix kernels do not interrupt a syscall in one thread when another
    thread closes its descriptor. This faithful boundary case keeps the inner
    listener open until its current bounded accept times out, then turns the
    requested close into the normal closed-listener error.
    """

    def __init__(self, inner: Listener) -> None:
        self._inner = inner
        self._closing = False
        self.accept_timeouts: list[float | None] = []

    def accept(self, timeout: float | None = None) -> Connection:
        self.accept_timeouts.append(timeout)
        try:
            return self._inner.accept(timeout)
        except TimeoutError:
            if not self._closing:
                raise
            self._inner.close()
            raise OSError(errno.EBADF, "listener closed") from None

    def close(self) -> None:
        self._closing = True


class _ListenerSwapNetwork:
    """A ``Network`` that passes every listener it binds through ``wrap``."""

    def __init__(self, inner: SimNetwork, wrap: typing.Callable[[Listener], Listener]) -> None:
        self._inner = inner
        self._wrap = wrap
        self.listener: Listener | None = None

    def listen(self, address: str) -> Listener:
        self.listener = self._wrap(self._inner.listen(address))
        return self.listener

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        return self._inner.connect(address, timeout)


def test_a_transient_accept_failure_is_logged_and_retried_not_treated_as_a_close(
    tmp_path: Path,
) -> None:
    """``EMFILE``/``ENFILE`` from ``accept()`` costs one retry, not the accept loop.

    Regression test for #2486: ``_accept_loop`` used to treat every
    ``OSError`` from ``accept()`` as proof the listener was closed and
    returned for good, so one transient failure permanently stopped the
    server from accepting any later connection. Here the very first
    ``accept()`` fails with ``EMFILE`` while the server is not closing; the
    next client must still be served, and ``accept()`` must have been called
    again.
    """
    threads = SimThreads(schedule_seed=0)
    socket_path = tmp_path / "control.sock"
    error = OSError(errno.EMFILE, "Too many open files")
    network = _ListenerSwapNetwork(
        SimNetwork(threads),
        lambda inner: _AcceptFaultListener(inner, frozenset({1}), error),
    )

    def main() -> None:
        parts = build_server_parts(None, threads=threads)
        with UnixJsonlServer(
            socket_path, parts.api, threads=threads, network=network, subscriptions=None
        ):
            client = network.connect(str(socket_path))
            client.send(_frame("query.snapshot"))
            reply = Response.model_validate_json(_read_line(client))
            assert reply.ok, reply
            client.close()
        assert isinstance(network.listener, _AcceptFaultListener)
        assert network.listener.calls >= 2, "accept() was not retried after the transient failure"

    threads.run(main)


def test_closing_the_server_ends_the_accept_loop_even_through_an_emfile_shaped_oserror(
    tmp_path: Path,
) -> None:
    """A deliberate close ends the accept loop whatever errno it surfaces as.

    Companion regression test for #2486: a deliberate close must still win
    even when the ``OSError`` it produces happens to look exactly like the
    transient failure the other branch now tolerates. The real
    ``listener.close()`` runs (through ``UnixJsonlServer.close``, which sets
    ``_closing`` before it), so the accept loop's choice of branch is proven
    to come from ``_closing``, not from the exception's errno.
    """
    threads = SimThreads(schedule_seed=0)
    socket_path = tmp_path / "control.sock"
    error = OSError(errno.EMFILE, "Too many open files")
    network = _ListenerSwapNetwork(
        SimNetwork(threads), lambda inner: _RelabeledErrorListener(inner, error)
    )

    def main() -> None:
        parts = build_server_parts(None, threads=threads)
        server = UnixJsonlServer(
            socket_path, parts.api, threads=threads, network=network, subscriptions=None
        )
        server.start()
        server.close()
        assert isinstance(network.listener, _RelabeledErrorListener)
        # Exactly the one accept() that was blocked when ``close`` ran. The
        # fixture raises a non-transport error on a retry, so a regression
        # fails deterministically instead of leaving ``close`` joined forever.
        assert network.listener.calls == 1

    threads.run(main)


def test_close_drains_accept_when_listener_close_does_not_interrupt_it(tmp_path: Path) -> None:
    """The accept poll bounds shutdown even when close cannot wake the syscall."""
    threads = SimThreads(schedule_seed=0)
    socket_path = tmp_path / "control.sock"
    network = _ListenerSwapNetwork(SimNetwork(threads), _DeferredCloseListener)

    def main() -> None:
        parts = build_server_parts(None, threads=threads)
        server = UnixJsonlServer(
            socket_path, parts.api, threads=threads, network=network, subscriptions=None
        )
        server.start()
        server.close()
        assert isinstance(network.listener, _DeferredCloseListener)
        assert network.listener.accept_timeouts
        assert all(timeout is not None for timeout in network.listener.accept_timeouts)
        parts.close()

    threads.run(main)
