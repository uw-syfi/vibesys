"""Relay one protocol connection between a byte stream pair and a run's Unix control socket.

The desktop reaches a remote run by running this relay over one ssh exec channel per
protocol connection (``WP-ROLES``): the channel's stdin and stdout on one side, the
run's control socket on the other. The relay copies bytes, not messages. The newline
framing of the Unix transport (``WP-NEWLINE``, ``WP-FRAMER``) passes through unchanged,
so the client keeps its own framer and the server sees an ordinary Unix client.
``docs/contributing/wire-protocol.md`` (``WP-STDIO-BRIDGE``) owns the semantics.

The module is a pure core and a thin shell. :func:`advance` folds what the two copy
loops observe (a write started or finished, a side ended, time passed) into a
:class:`RelayState` and decides how the relay ends; :func:`run_bridge` connects, runs
one copy loop per direction on a :class:`~vs_sim.api.Threads` worker, and supervises
them with that fold. Time comes only from ``Threads.now`` and its waits, so a
simulator drives every deadline.

Startup cost matters because the relay runs once per connection: this module imports
only the standard library and ``vs_sim.api``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from vs_sim.api import Connection, Threads

CHUNK_BYTES: Final = 64 * 1024
"""The most bytes one read takes from either side."""


class Peer(StrEnum):
    """The two ends the relay joins."""

    CLIENT = "client"
    """The stdin and stdout side (the ssh channel)."""
    SERVER = "server"
    """The run's control socket."""


class Flow(StrEnum):
    """Which way a copy loop was moving bytes when its side ended."""

    FROM_PEER = "from_peer"
    """Reading from the peer."""
    TO_PEER = "to_peer"
    """Writing to the peer."""


class EndReason(StrEnum):
    """Why a side stopped carrying bytes."""

    CLOSED = "closed"
    """End of stream, a broken pipe, or a reset: the peer went away."""
    FAILED = "failed"
    """Any other I/O error."""


class BridgeOutcome(StrEnum):
    """How one relay ended; each value has its own exit status (:data:`EXIT_STATUS`)."""

    CLIENT_CLOSED = "client_closed"
    """Stdin reached end of stream or stdout was closed: the client went away."""
    SERVER_CLOSED = "server_closed"
    """The server closed the connection; everything it sent was relayed first."""
    RUN_GONE = "run_gone"
    """Nothing listens at the socket path (no file, or a stale one): the run is not there."""
    CONNECT_DENIED = "connect_denied"
    """The socket exists but this user may not connect to it."""
    CONNECT_FAILED = "connect_failed"
    """Connecting failed another way, including the connect deadline running out."""
    SERVER_STALLED = "server_stalled"
    """A write toward the server made no progress within the write deadline."""
    CLIENT_STALLED = "client_stalled"
    """A write toward stdout made no progress within the write deadline."""
    SERVER_FAILED = "server_failed"
    """Reading from or writing to the socket failed with an error other than a close."""
    CLIENT_FAILED = "client_failed"
    """Reading stdin or writing stdout failed with an error other than a close."""


EXIT_STATUS: Final[dict[BridgeOutcome, int]] = {
    BridgeOutcome.CLIENT_CLOSED: 0,
    BridgeOutcome.SERVER_CLOSED: 3,
    BridgeOutcome.RUN_GONE: 4,
    BridgeOutcome.CONNECT_DENIED: 5,
    BridgeOutcome.CONNECT_FAILED: 6,
    BridgeOutcome.SERVER_STALLED: 7,
    BridgeOutcome.CLIENT_STALLED: 8,
    BridgeOutcome.SERVER_FAILED: 9,
    BridgeOutcome.CLIENT_FAILED: 10,
}
"""Process exit status per outcome. 1 stays an uncaught error and 2 a usage error."""


@dataclass(frozen=True)
class BridgeLimits:
    """Every bound the relay applies, stated rather than inherited."""

    write_deadline_seconds: float = 40.0
    """How long one write toward either peer may make no progress before the relay ends.

    The same ceiling as the WebSocket gateway's ``write_deadline_seconds``, so a dead
    remote client pins server resources no longer on this path than on that one.
    """
    connect_timeout_seconds: float = 5.0
    """How long connecting to the control socket may take."""


# ----- the pure core -----


@dataclass(frozen=True)
class WriteStarted:
    """A copy loop began writing a chunk toward ``peer``."""

    peer: Peer
    at: float


@dataclass(frozen=True)
class WriteFinished:
    """The write toward ``peer`` completed."""

    peer: Peer


@dataclass(frozen=True)
class Ended:
    """A copy loop stopped because ``peer`` ended while it moved bytes ``flow``."""

    peer: Peer
    flow: Flow
    reason: EndReason


@dataclass(frozen=True)
class Tick:
    """Time has reached ``at``."""

    at: float


type RelayEvent = WriteStarted | WriteFinished | Ended | Tick


@dataclass(frozen=True)
class RelayState:
    """What the relay knows: writes in flight and, once decided, how it ends."""

    client_write_since: float | None = None
    server_write_since: float | None = None
    outcome: BridgeOutcome | None = None


_STALLED: Final = {
    Peer.CLIENT: BridgeOutcome.CLIENT_STALLED,
    Peer.SERVER: BridgeOutcome.SERVER_STALLED,
}
_CLOSED: Final = {
    Peer.CLIENT: BridgeOutcome.CLIENT_CLOSED,
    Peer.SERVER: BridgeOutcome.SERVER_CLOSED,
}
_FAILED: Final = {
    Peer.CLIENT: BridgeOutcome.CLIENT_FAILED,
    Peer.SERVER: BridgeOutcome.SERVER_FAILED,
}


def _pending(state: RelayState) -> dict[Peer, float]:
    since = {Peer.CLIENT: state.client_write_since, Peer.SERVER: state.server_write_since}
    return {peer: at for peer, at in since.items() if at is not None}


def _with_pending(state: RelayState, peer: Peer, since: float | None) -> RelayState:
    if peer is Peer.CLIENT:
        return replace(state, client_write_since=since)
    return replace(state, server_write_since=since)


def ending(event: Ended) -> BridgeOutcome | None:
    """The outcome a side's end decides, or ``None`` when it decides nothing yet.

    A write toward the server that finds it closed is not the end: the server may have
    sent a final frame (a ``protocol_error`` before its close, ``WP-PROTOCOL-ERROR``)
    that the server-to-client loop has not relayed yet. That loop reads the server's
    end of stream after the frame and decides ``server_closed`` then.
    """
    if event.reason is EndReason.FAILED:
        return _FAILED[event.peer]
    if event.peer is Peer.SERVER and event.flow is Flow.TO_PEER:
        return None
    return _CLOSED[event.peer]


def advance(state: RelayState, event: RelayEvent, limits: BridgeLimits) -> RelayState:
    """Fold one observation into the state. The first decided outcome is final."""
    if state.outcome is not None:
        return state
    match event:
        case WriteStarted(peer=peer, at=at):
            return _with_pending(state, peer, at)
        case WriteFinished(peer=peer):
            return _with_pending(state, peer, None)
        case Ended():
            outcome = ending(event)
            return state if outcome is None else replace(state, outcome=outcome)
        case Tick(at=at):
            overdue = [
                peer
                for peer, since in _pending(state).items()
                if at - since >= limits.write_deadline_seconds
            ]
            if not overdue:
                return state
            first = min(overdue, key=lambda peer: _pending(state)[peer])
            return replace(state, outcome=_STALLED[first])


def next_deadline(state: RelayState, limits: BridgeLimits) -> float | None:
    """When the oldest write in flight runs out of time; ``None`` when none is in flight."""
    pending = _pending(state)
    if state.outcome is not None or not pending:
        return None
    return min(pending.values()) + limits.write_deadline_seconds


# ----- the shell -----


class ByteSource(Protocol):
    """Where a copy loop reads."""

    def read(self, max_bytes: int) -> bytes:
        """Up to ``max_bytes`` bytes, at least one; ``b""`` at end of stream. ``OSError`` on failure."""
        ...


class ByteSink(Protocol):
    """Where a copy loop writes."""

    def write(self, data: bytes) -> None:
        """Write all of ``data``, blocking while the reader is behind. ``OSError`` on failure."""
        ...


class Dialer(Protocol):
    """Where the relay connects; :class:`vs_sim.api.Network` is one."""

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        """Dial ``address``. ``ConnectionRefusedError`` when nothing listens there."""
        ...


class _ConnectionSource:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def read(self, max_bytes: int) -> bytes:
        return self._connection.recv(max_bytes)


class _ConnectionSink:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def write(self, data: bytes) -> None:
        self._connection.send(data)


def _reason(error: OSError) -> EndReason:
    if isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
        return EndReason.CLOSED
    return EndReason.FAILED


@dataclass(frozen=True)
class ClientStreams:
    """The client side of the relay: the ssh channel's stdin and stdout in production."""

    incoming: ByteSource
    """What the client sends; relayed to the server."""
    outgoing: ByteSink
    """Where the server's bytes go."""


@dataclass(frozen=True)
class BridgeResult:
    """How the relay ended, with a human-readable detail for the error line."""

    outcome: BridgeOutcome
    detail: str

    @property
    def exit_status(self) -> int:
        """The process exit status for this outcome."""
        return EXIT_STATUS[self.outcome]


class _Relay:
    """The supervised state shared by the two copy loops."""

    def __init__(self, threads: Threads, limits: BridgeLimits) -> None:
        self._threads = threads
        self._limits = limits
        self._cond = threads.condition()
        self._state = RelayState()

    def report(self, event: RelayEvent) -> None:
        with self._cond:
            self._state = advance(self._state, event, self._limits)
            self._cond.notify_all()

    def copy(self, source: ByteSource, source_peer: Peer, sink: ByteSink, sink_peer: Peer) -> None:
        """Copy until either side ends; report every write and the end."""
        while True:
            try:
                chunk = source.read(CHUNK_BYTES)
            except OSError as error:
                self.report(Ended(source_peer, Flow.FROM_PEER, _reason(error)))
                return
            if not chunk:
                self.report(Ended(source_peer, Flow.FROM_PEER, EndReason.CLOSED))
                return
            self.report(WriteStarted(sink_peer, self._threads.now()))
            try:
                sink.write(chunk)
            except OSError as error:
                self.report(Ended(sink_peer, Flow.TO_PEER, _reason(error)))
                return
            self.report(WriteFinished(sink_peer))

    def supervise(self) -> BridgeOutcome:
        """Wait until the fold decides an outcome, ticking at each write deadline."""
        with self._cond:
            while self._state.outcome is None:
                deadline = next_deadline(self._state, self._limits)
                timeout = None if deadline is None else max(deadline - self._threads.now(), 0.0)
                self._cond.wait(timeout)
                self._state = advance(self._state, Tick(self._threads.now()), self._limits)
            return self._state.outcome


_DETAIL: Final = {
    BridgeOutcome.CLIENT_CLOSED: "the client closed its side",
    BridgeOutcome.SERVER_CLOSED: "the server closed the connection",
    BridgeOutcome.SERVER_STALLED: "the server read nothing for {deadline:g}s",
    BridgeOutcome.CLIENT_STALLED: "stdout was not drained for {deadline:g}s",
    BridgeOutcome.SERVER_FAILED: "the control socket failed",
    BridgeOutcome.CLIENT_FAILED: "stdin or stdout failed",
}


def connect_outcome(error: OSError) -> BridgeOutcome:
    """Classify a failed connect: the run is gone, access is denied, or the transport broke."""
    if isinstance(error, (ConnectionRefusedError, FileNotFoundError)):
        return BridgeOutcome.RUN_GONE
    if isinstance(error, PermissionError):
        return BridgeOutcome.CONNECT_DENIED
    return BridgeOutcome.CONNECT_FAILED


def run_bridge(
    *,
    network: Dialer,
    address: str,
    client: ClientStreams,
    threads: Threads,
    limits: BridgeLimits | None = None,
) -> BridgeResult:
    """Connect to ``address`` and relay until one side ends or a write stalls.

    Returns once the outcome is decided and the socket is closed. A copy loop may still
    be blocked on stdin or on a stalled stdout write; the caller ends the process.
    """
    limits = limits or BridgeLimits()
    try:
        connection = network.connect(address, timeout=limits.connect_timeout_seconds)
    except OSError as error:
        return BridgeResult(
            connect_outcome(error), f"connecting to {address}: {error.strerror or error}"
        )
    relay = _Relay(threads, limits)
    threads.spawn(
        lambda: relay.copy(client.incoming, Peer.CLIENT, _ConnectionSink(connection), Peer.SERVER),
        name="stdio-bridge-upstream",
    )
    threads.spawn(
        lambda: relay.copy(
            _ConnectionSource(connection), Peer.SERVER, client.outgoing, Peer.CLIENT
        ),
        name="stdio-bridge-downstream",
    )
    try:
        outcome = relay.supervise()
    finally:
        connection.close()
    return BridgeResult(outcome, _DETAIL[outcome].format(deadline=limits.write_deadline_seconds))
