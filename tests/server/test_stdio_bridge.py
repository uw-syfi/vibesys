"""The stdio bridge's relay, on the simulated network and threads.

The pure fold (:func:`advance`) is checked directly over arbitrary event sequences. The
shell (:func:`run_bridge`) runs against :class:`SimNetwork` with Fakes for stdin and
stdout, under seeded schedules, so chunk boundaries, ends and stalls interleave in many
orders while every deadline is virtual time. The same relay against a real server
process is exercised in ``tests/e2e/test_stdio_bridge.py``.
"""

from __future__ import annotations

import errno
import itertools
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from server.stdio_bridge import (
    EXIT_STATUS,
    BridgeLimits,
    BridgeOutcome,
    BridgeResult,
    ClientStreams,
    Ended,
    EndReason,
    Flow,
    Peer,
    RelayEvent,
    RelayState,
    Tick,
    WriteFinished,
    WriteStarted,
    advance,
    ending,
    next_deadline,
    run_bridge,
)
from server.stdio_bridge_report import BridgeReport
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads

if TYPE_CHECKING:
    from vs_sim.api import Connection, Listener

_LIMITS = BridgeLimits()
_DEADLINE = _LIMITS.write_deadline_seconds
_ADDRESS = "run.sock"

# ----- the pure fold -----

_peers = st.sampled_from(Peer)
_events: st.SearchStrategy[RelayEvent] = st.one_of(
    st.builds(WriteStarted, _peers, st.floats(0, 200)),
    st.builds(WriteFinished, _peers),
    st.builds(Ended, _peers, st.sampled_from(Flow), st.sampled_from(EndReason)),
    st.builds(Tick, st.floats(0, 200)),
)


def _fold(events: list[RelayEvent]) -> list[RelayState]:
    states = [RelayState()]
    for event in events:
        states.append(advance(states[-1], event, _LIMITS))
    return states


@given(st.lists(_events, max_size=30))
def test_the_first_decided_outcome_is_final(events: list[RelayEvent]) -> None:
    decided = {state.outcome for state in _fold(events) if state.outcome is not None}
    assert len(decided) <= 1


@given(st.lists(_events, max_size=30), st.floats(1e-3, 100))
def test_a_write_in_flight_stalls_exactly_at_its_deadline(
    events: list[RelayEvent], early_by: float
) -> None:
    state = _fold(events)[-1]
    deadline = next_deadline(state, _LIMITS)
    if deadline is None:
        assert state.outcome is not None or (
            state.client_write_since is None and state.server_write_since is None
        )
        return
    early = advance(state, Tick(deadline - early_by), _LIMITS)
    assert early.outcome is None
    stalled = advance(state, Tick(deadline), _LIMITS)
    assert stalled.outcome in {BridgeOutcome.CLIENT_STALLED, BridgeOutcome.SERVER_STALLED}


@pytest.mark.parametrize("peer", list(Peer))
@pytest.mark.parametrize("flow", list(Flow))
@pytest.mark.parametrize("reason", list(EndReason))
def test_every_end_has_one_classification(peer: Peer, flow: Flow, reason: EndReason) -> None:
    decided = ending(Ended(peer, flow, reason))
    if reason is EndReason.FAILED:
        assert (
            decided
            is {Peer.CLIENT: BridgeOutcome.CLIENT_FAILED, Peer.SERVER: BridgeOutcome.SERVER_FAILED}[
                peer
            ]
        )
    elif peer is Peer.SERVER and flow is Flow.TO_PEER:
        # The server's last frame may still be in flight toward the client.
        assert decided is None
    else:
        assert (
            decided
            is {Peer.CLIENT: BridgeOutcome.CLIENT_CLOSED, Peer.SERVER: BridgeOutcome.SERVER_CLOSED}[
                peer
            ]
        )


def test_every_outcome_has_its_own_exit_status_and_only_a_client_close_is_zero() -> None:
    assert set(EXIT_STATUS) == set(BridgeOutcome)
    assert len(set(EXIT_STATUS.values())) == len(BridgeOutcome)
    assert [o for o, status in EXIT_STATUS.items() if status == 0] == [BridgeOutcome.CLIENT_CLOSED]
    assert {1, 2}.isdisjoint(EXIT_STATUS.values())


@pytest.mark.parametrize("outcome", list(BridgeOutcome))
def test_the_report_line_is_one_json_object_naming_outcome_and_status(
    outcome: BridgeOutcome,
) -> None:
    line = BridgeReport.of(BridgeResult(outcome, "why")).line()
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert json.loads(line) == {
        "outcome": outcome.value,
        "exit_status": EXIT_STATUS[outcome],
        "detail": "why",
    }
    assert BridgeReport.model_validate_json(line).outcome is outcome


# ----- the shell, on simulated threads and network -----


@dataclass
class _Stdin:
    """Hands out scripted chunks, then waits until ``finish`` before its end of stream."""

    threads: SimThreads
    chunks: list[bytes]
    error: OSError | None = None
    finished: bool = False

    def __post_init__(self) -> None:
        self._cond = self.threads.condition()

    def finish(self) -> None:
        with self._cond:
            self.finished = True
            self._cond.notify_all()

    def read(self, max_bytes: int) -> bytes:
        with self._cond:
            if self.chunks:
                chunk = self.chunks.pop(0)
                if len(chunk) > max_bytes:
                    self.chunks.insert(0, chunk[max_bytes:])
                return chunk[:max_bytes]
            self._cond.wait_for(lambda: self.finished)
            if self.error is not None:
                raise self.error
            return b""


@dataclass
class _Stdout:
    """Records what the relay writes; a stalled one never returns from a write."""

    threads: SimThreads
    stalled: bool = False
    data: bytearray = field(default_factory=bytearray)

    def __post_init__(self) -> None:
        self._cond = self.threads.condition()

    def write(self, data: bytes) -> None:
        with self._cond:
            if self.stalled:
                self._cond.wait_for(lambda: False)
            self.data += data
            self._cond.notify_all()

    def wait_for(self, size: int) -> None:
        with self._cond:
            assert self._cond.wait_for(lambda: len(self.data) >= size, HANG_GUARD_S)


def _split(data: bytes, cuts: list[int]) -> list[bytes]:
    points = sorted({cut % (len(data) + 1) for cut in cuts} | {0, len(data)})
    return [data[a:b] for a, b in itertools.pairwise(points) if b > a]


def _receive_exactly(connection: Connection, size: int) -> bytes:
    received = bytearray()
    while len(received) < size:
        chunk = connection.recv(size - len(received), HANG_GUARD_S)
        assert chunk, "the relay closed before the expected bytes"
        received += chunk
    return bytes(received)


_frames = st.lists(
    st.binary(max_size=40).map(lambda body: body.replace(b"\n", b" ") + b"\n"),
    min_size=1,
    max_size=6,
).map(b"".join)
_cuts = st.lists(st.integers(0, 10_000), max_size=8)
_seeds = st.integers(0, 2**16)


@settings(max_examples=40, deadline=None)
@given(_frames, _cuts, _frames, _cuts, _seeds)
def test_bytes_cross_unchanged_both_ways_and_a_client_close_closes_the_socket(
    upstream: bytes, up_cuts: list[int], downstream: bytes, down_cuts: list[int], seed: int
) -> None:
    threads = SimThreads(schedule_seed=seed)
    network = SimNetwork(threads)
    stdin = _Stdin(threads, _split(upstream, up_cuts))
    stdout = _Stdout(threads)
    seen: dict[str, bytes] = {}

    def serve(listener: Listener) -> None:
        connection = listener.accept(HANG_GUARD_S)
        seen["upstream"] = _receive_exactly(connection, len(upstream))
        for chunk in _split(downstream, down_cuts):
            connection.send(chunk)
        stdout.wait_for(len(downstream))
        stdin.finish()
        seen["after_close"] = connection.recv(1, HANG_GUARD_S)

    def main() -> BridgeResult:
        listener = network.listen(_ADDRESS)
        server = threads.spawn(lambda: serve(listener), name="server")
        result = run_bridge(
            network=network, address=_ADDRESS, client=ClientStreams(stdin, stdout), threads=threads
        )
        server.join(HANG_GUARD_S)
        return result

    result = threads.run(main)
    assert result.outcome is BridgeOutcome.CLIENT_CLOSED
    assert seen == {"upstream": upstream, "after_close": b""}
    assert bytes(stdout.data) == downstream


@settings(max_examples=40, deadline=None)
@given(_frames, _cuts, _seeds)
def test_a_server_close_is_reported_only_after_its_last_frame_reaches_stdout(
    final: bytes, cuts: list[int], seed: int
) -> None:
    """A protocol error is a frame then a close (WP-PROTOCOL-ERROR); the frame is never lost.

    The client keeps writing after the server closed, so the upstream loop finds the
    socket closed in some schedules before the downstream loop relays the final frame.
    """
    threads = SimThreads(schedule_seed=seed)
    network = SimNetwork(threads)
    stdin = _Stdin(threads, [b'{"type":"command.pause"}\n'] * 4)
    stdout = _Stdout(threads)

    def serve(listener: Listener) -> None:
        connection = listener.accept(HANG_GUARD_S)
        connection.recv(1, HANG_GUARD_S)
        for chunk in _split(final, cuts):
            connection.send(chunk)
        connection.close()

    def main() -> BridgeResult:
        listener = network.listen(_ADDRESS)
        threads.spawn(lambda: serve(listener), name="server")
        return run_bridge(
            network=network, address=_ADDRESS, client=ClientStreams(stdin, stdout), threads=threads
        )

    result = threads.run(main)
    assert result.outcome is BridgeOutcome.SERVER_CLOSED
    assert result.exit_status == EXIT_STATUS[BridgeOutcome.SERVER_CLOSED]
    assert bytes(stdout.data) == final


def test_a_stdout_that_stops_draining_ends_the_relay_at_the_write_deadline() -> None:
    threads = SimThreads()
    network = SimNetwork(threads)
    stdin = _Stdin(threads, [])
    stdout = _Stdout(threads, stalled=True)
    peer: dict[str, bytes] = {}

    def serve(listener: Listener) -> None:
        connection = listener.accept(HANG_GUARD_S)
        connection.send(b'{"type":"subscribed"}\n')
        peer["after_stall"] = connection.recv(1, HANG_GUARD_S * 4)

    def main() -> tuple[BridgeResult, float]:
        started_at = threads.now()
        listener = network.listen(_ADDRESS)
        server = threads.spawn(lambda: serve(listener), name="server")
        result = run_bridge(
            network=network, address=_ADDRESS, client=ClientStreams(stdin, stdout), threads=threads
        )
        ended_at = threads.now() - started_at
        server.join(HANG_GUARD_S)
        return result, ended_at

    result, ended_at = threads.run(main)
    assert result.outcome is BridgeOutcome.CLIENT_STALLED
    assert ended_at == pytest.approx(_DEADLINE)
    # The server sees the client gone instead of being pinned behind it.
    assert peer == {"after_stall": b""}


class _UnreadSocket:
    """A network whose connections never finish a send, like a server that stopped reading."""

    def __init__(self, threads: SimThreads, network: SimNetwork) -> None:
        self._threads = threads
        self._network = network

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        return _NeverSends(self._threads, self._network.connect(address, timeout))


class _NeverSends:
    def __init__(self, threads: SimThreads, inner: Connection) -> None:
        self._cond = threads.condition()
        self._inner = inner
        self._closed = False
        self.unsent = b""

    def send(self, data: bytes) -> None:
        with self._cond:
            self.unsent = data
            self._cond.wait_for(lambda: self._closed)
            raise BrokenPipeError(errno.EPIPE, "closed while stalled")

    def recv(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.recv(max_bytes, timeout)

    def peer_closed(self) -> bool:
        return self._inner.peer_closed()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._inner.close()


def test_a_server_that_stops_reading_ends_the_relay_at_the_write_deadline() -> None:
    threads = SimThreads()
    network = SimNetwork(threads)
    stdin = _Stdin(threads, [b'{"type":"command.pause"}\n'])
    stdout = _Stdout(threads)

    def main() -> tuple[BridgeResult, float]:
        started_at = threads.now()
        listener = network.listen(_ADDRESS)
        result = run_bridge(
            network=_UnreadSocket(threads, network),
            address=_ADDRESS,
            client=ClientStreams(stdin, stdout),
            threads=threads,
        )
        listener.close()
        return result, threads.now() - started_at

    result, ended_at = threads.run(main)
    assert result.outcome is BridgeOutcome.SERVER_STALLED
    assert ended_at == pytest.approx(_DEADLINE)
    assert "40s" in result.detail
    assert result.exit_status == EXIT_STATUS[BridgeOutcome.SERVER_STALLED]


def test_a_stdin_failure_is_a_client_failure() -> None:
    threads = SimThreads()
    network = SimNetwork(threads)
    stdin = _Stdin(threads, [], error=OSError(errno.EIO, "input/output error"), finished=True)

    def main() -> BridgeResult:
        listener = network.listen(_ADDRESS)
        try:
            return run_bridge(
                network=network,
                address=_ADDRESS,
                client=ClientStreams(stdin, _Stdout(threads)),
                threads=threads,
            )
        finally:
            listener.close()

    assert threads.run(main).outcome is BridgeOutcome.CLIENT_FAILED


class _RefusingNetwork:
    def __init__(self, error: OSError) -> None:
        self._error = error
        self.dials: list[tuple[str, float | None]] = []

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        self.dials.append((address, timeout))
        raise self._error


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ConnectionRefusedError(errno.ECONNREFUSED, "nothing listens"), BridgeOutcome.RUN_GONE),
        (FileNotFoundError(errno.ENOENT, "no such file"), BridgeOutcome.RUN_GONE),
        (PermissionError(errno.EACCES, "permission denied"), BridgeOutcome.CONNECT_DENIED),
        (TimeoutError("timed out"), BridgeOutcome.CONNECT_FAILED),
        (OSError(errno.ENOTSOCK, "not a socket"), BridgeOutcome.CONNECT_FAILED),
    ],
)
def test_a_failed_connect_names_why_without_relaying(
    error: OSError, outcome: BridgeOutcome
) -> None:
    threads = SimThreads()
    stdin = _Stdin(threads, [b"never read\n"])
    network = _RefusingNetwork(error)
    result = threads.run(
        lambda: run_bridge(
            network=network,
            address=_ADDRESS,
            client=ClientStreams(stdin, _Stdout(threads)),
            threads=threads,
        )
    )
    assert result.outcome is outcome
    assert _ADDRESS in result.detail
    assert stdin.chunks == [b"never read\n"]
    assert network.dials == [(_ADDRESS, _LIMITS.connect_timeout_seconds)]


def test_dialing_an_unbound_address_on_the_simulated_network_is_run_gone() -> None:
    threads = SimThreads()
    network = SimNetwork(threads)
    result = threads.run(
        lambda: run_bridge(
            network=network,
            address=_ADDRESS,
            client=ClientStreams(_Stdin(threads, []), _Stdout(threads)),
            threads=threads,
        )
    )
    assert result.outcome is BridgeOutcome.RUN_GONE
