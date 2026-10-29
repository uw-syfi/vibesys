"""Relay one protocol connection between a byte stream pair and a run's Unix control socket.

The desktop reaches a remote run by running this relay over one ssh exec channel per
protocol connection (``WP-ROLES``): the channel's stdin and stdout on one side, the
run's control socket on the other. The relay copies bytes, not messages. The newline
framing of the Unix transport (``WP-NEWLINE``, ``WP-FRAMER``) passes through unchanged,
so the client keeps its own framer and the server sees an ordinary Unix client.
``docs/contributing/wire-protocol.md`` (section "The relay") owns the semantics.

The module is a pure core and a thin shell. :func:`advance` folds what the two copy
loops observe (a write started or finished, a read made progress, a side ended, time
passed) into a :class:`RelayState` and decides how the relay ends; :func:`run_bridge`
connects, runs one copy loop per direction on a :class:`~vs_sim.api.Threads` worker, and
supervises them with that fold. Time comes only from ``Threads.now`` and its waits, so a
simulator drives every deadline.

Startup cost matters because the relay runs once per connection: this module imports
only the standard library and ``vs_sim.api``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from server.host_api import ChannelEnd, ChannelEndOutcome
from vs_sim.api import WorkerStartRefusedError

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Condition, Connection, ReservableThreads, WorkerReservation

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


EXIT_STATUS: Final[dict[ChannelEndOutcome, int]] = {
    ChannelEndOutcome.CLIENT_CLOSED: 0,
    ChannelEndOutcome.SERVER_CLOSED: 3,
    ChannelEndOutcome.RUN_GONE: 4,
    ChannelEndOutcome.CONNECT_DENIED: 5,
    ChannelEndOutcome.CONNECT_FAILED: 6,
    ChannelEndOutcome.SERVER_STALLED: 7,
    ChannelEndOutcome.CLIENT_STALLED: 8,
    ChannelEndOutcome.SERVER_FAILED: 9,
    ChannelEndOutcome.CLIENT_FAILED: 10,
    ChannelEndOutcome.SERVER_UNRESPONSIVE: 11,
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
    read_deadline_seconds: float | None = 7 * 24 * 60 * 60.0
    """How long the control socket may send nothing before the relay ends. ``None`` disables it.

    Only the server peer's read is checked: the client peer is the ssh exec channel's
    stdin, and ssh's own keepalive already reaps a client that neither drains its socket
    nor answers pings (``docs/contributing/wire-protocol.md``'s idle-hop paragraph). The
    control socket (``AF_UNIX``) has no such keepalive, so a server that stops sending
    without closing wedges this relay forever without this deadline.

    A provider capacity pause can legitimately leave the control socket silent for as
    long as the run waits: ``[agent.quota]``'s shipped default policy (``pause``, see
    ``vibesys.config.QuotaCfg``) waits for the operator with no bound at all, and even
    the self-resuming policies (``wait``/``fallback``) wait without a bound unless
    ``wait_seconds`` is set. So no finite deadline here can be "longer than every
    legitimate wait" the way a write deadline can; this one instead draws the line at a
    duration an operator is unlikely to leave a run paused past without checking on it
    (a week), so a run idling past it just means its desktop window reconnects; the
    underlying run is untouched, since closing one relayed connection only ends that
    connection, not the run (``src/server/relay.py`` module docstring).
    """


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
class ReadProgress:
    """A copy loop read a non-empty chunk from ``peer``."""

    peer: Peer
    at: float


@dataclass(frozen=True)
class Tick:
    """Time has reached ``at``."""

    at: float


type RelayEvent = WriteStarted | WriteFinished | Ended | ReadProgress | Tick


@dataclass(frozen=True)
class RelayState:
    """What the relay knows: writes in flight, each peer's last read, and how it ends."""

    client_write_since: float | None = None
    server_write_since: float | None = None
    client_read_at: float | None = None
    """When the client peer (stdin) was last read from; seeded at the relay's start."""
    server_read_at: float | None = None
    """When the server peer (the control socket) was last read from; seeded at the relay's start."""
    outcome: ChannelEndOutcome | None = None


_STALLED: Final = {
    Peer.CLIENT: ChannelEndOutcome.CLIENT_STALLED,
    Peer.SERVER: ChannelEndOutcome.SERVER_STALLED,
}
_CLOSED: Final = {
    Peer.CLIENT: ChannelEndOutcome.CLIENT_CLOSED,
    Peer.SERVER: ChannelEndOutcome.SERVER_CLOSED,
}
_FAILED: Final = {
    Peer.CLIENT: ChannelEndOutcome.CLIENT_FAILED,
    Peer.SERVER: ChannelEndOutcome.SERVER_FAILED,
}


def _pending(state: RelayState) -> dict[Peer, float]:
    since = {Peer.CLIENT: state.client_write_since, Peer.SERVER: state.server_write_since}
    return {peer: at for peer, at in since.items() if at is not None}


def _with_pending(state: RelayState, peer: Peer, since: float | None) -> RelayState:
    if peer is Peer.CLIENT:
        return replace(state, client_write_since=since)
    return replace(state, server_write_since=since)


def _with_read(state: RelayState, peer: Peer, at: float) -> RelayState:
    if peer is Peer.CLIENT:
        return replace(state, client_read_at=at)
    return replace(state, server_read_at=at)


def ending(event: Ended) -> ChannelEndOutcome | None:
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
            # A `None` outcome means the failed write is over, not stalled: only the end
            # of the server's stream (or another deadline) decides from here.
            return (
                replace(state, outcome=outcome)
                if outcome is not None
                else _with_pending(state, event.peer, None)
            )
        case ReadProgress(peer=peer, at=at):
            return _with_read(state, peer, at)
        case Tick(at=at):
            return _tick(state, at, limits)


def _deadlines(state: RelayState, limits: BridgeLimits) -> dict[ChannelEndOutcome, float]:
    """Every deadline the relay is tracking, keyed by the outcome it would decide.

    Only the server peer's read feeds a deadline here (see ``BridgeLimits.read_deadline_seconds``);
    ``state.client_read_at`` is tracked for the same shape but decides nothing, since ssh's own
    keepalive already reaps a silently-wedged client.
    """
    deadlines = {
        _STALLED[peer]: since + limits.write_deadline_seconds
        for peer, since in _pending(state).items()
    }
    if limits.read_deadline_seconds is not None and state.server_read_at is not None:
        deadlines[ChannelEndOutcome.SERVER_UNRESPONSIVE] = (
            state.server_read_at + limits.read_deadline_seconds
        )
    return deadlines


def _tick(state: RelayState, at: float, limits: BridgeLimits) -> RelayState:
    """Decide whichever tracked deadline is earliest, once it has run out of time."""
    # Compare against the same sum ``next_deadline`` returns: ``at - since`` can round
    # below the deadline at the instant the supervisor wakes for it, and the supervisor
    # would then spin on a zero wait without ever deciding.
    deadlines = _deadlines(state, limits)
    overdue = {outcome: deadline for outcome, deadline in deadlines.items() if at >= deadline}
    if not overdue:
        return state
    return replace(state, outcome=min(overdue, key=overdue.__getitem__))


def next_deadline(state: RelayState, limits: BridgeLimits) -> float | None:
    """When the earliest tracked deadline runs out of time; ``None`` when none is tracked."""
    if state.outcome is not None:
        return None
    deadlines = _deadlines(state, limits)
    return min(deadlines.values()) if deadlines else None


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


class FdSource:
    """Unbuffered reads from a file descriptor, so a frame is handled as soon as it arrives."""

    def __init__(self, fd: int) -> None:
        """Read from ``fd``."""
        self._fd = fd

    def read(self, max_bytes: int) -> bytes:
        """Up to ``max_bytes`` bytes; ``b""`` at end of stream."""
        return os.read(self._fd, max_bytes)


class FdSink:
    """Unbuffered writes to a file descriptor, so a stalled reader blocks here, not in a buffer."""

    def __init__(self, fd: int) -> None:
        """Write to ``fd``."""
        self._fd = fd

    def write(self, data: bytes) -> None:
        """Write all of ``data``."""
        view = memoryview(data)
        while view:
            view = view[os.write(self._fd, view) :]


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

    outcome: ChannelEndOutcome
    detail: str

    @property
    def exit_status(self) -> int:
        """The process exit status for this outcome."""
        return EXIT_STATUS[self.outcome]

    def channel_end(self) -> ChannelEnd:
        """The ``ChannelEnd`` line a relayed channel writes when its status is not ``0``."""
        return ChannelEnd(outcome=self.outcome, exit_status=self.exit_status, detail=self.detail)


class _Relay:
    """The supervised state shared by the two copy loops."""

    def __init__(self, threads: ReservableThreads, limits: BridgeLimits) -> None:
        self._threads = threads
        self._limits = limits
        self._cond = threads.condition()
        # Seed both reads to now, not None, so a relay that never receives anything from
        # either peer is still bounded by the read deadline from the start.
        now = threads.now()
        self._state = RelayState(client_read_at=now, server_read_at=now)

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
            self.report(ReadProgress(source_peer, self._threads.now()))
            self.report(WriteStarted(sink_peer, self._threads.now()))
            try:
                sink.write(chunk)
            except OSError as error:
                self.report(Ended(sink_peer, Flow.TO_PEER, _reason(error)))
                return
            self.report(WriteFinished(sink_peer))

    def startup_gate(self) -> _StartupGate:
        """A startup gate observed under the supervisor's condition."""
        return _StartupGate(self._cond)

    def supervise(self, startup: _StartupGate) -> ChannelEndOutcome:
        """Commit startup and supervise without an interruptible ownership handoff."""
        with self._cond:
            try:
                startup.decide_held(commit=True)
                while self._state.outcome is None:
                    deadline = next_deadline(self._state, self._limits)
                    timeout = None if deadline is None else max(deadline - self._threads.now(), 0.0)
                    self._cond.wait(timeout)
                    self._state = advance(self._state, Tick(self._threads.now()), self._limits)
            except BaseException:
                # Until a worker reacquires this same condition and observes the
                # decision, commit is private to this owner. An interruption before
                # the first supervision wait releases the lock can still become an
                # abort, so no blocked copy worker escapes the transition.
                startup.abort_if_unobserved_held()
                raise
            else:
                return self._state.outcome


class _StartupGate:
    """Commit both copy workers together, or let either one settle without touching I/O."""

    def __init__(self, condition: Condition) -> None:
        self._condition = condition
        self._committed: bool | None = None
        self._published = False
        self._observed = False

    def wait(self) -> bool:
        """Wait for the owner to commit or abort; whether copying may start."""
        with self._condition:
            self._condition.wait_for(lambda: self._published)
            self._observed = True
            return self._committed is True

    def decide(self, *, commit: bool) -> bool:
        """Publish the first startup decision and idempotently notify every waiter."""
        with self._condition:
            return self.decide_held(commit=commit)

    def decide_held(self, *, commit: bool) -> bool:
        """Publish while the caller holds the gate condition."""
        if not self._published:
            self._committed = commit
            # Wakeups are prepared before publication. Waiters cannot reacquire the
            # held condition until ``_published`` is true; if notification raises,
            # cleanup may instead publish abort and notify again.
            self._condition.notify_all()
            self._published = True
        else:
            # Notification remains retryable after publication so an interruption
            # after the fact became visible cannot strand an already waiting worker.
            self._condition.notify_all()
        return self._committed is True

    def abort_if_unobserved_held(self) -> None:
        """Replace an unobserved commit with abort while its condition is still held."""
        if not self._observed:
            self._committed = False
            self._published = False


_DETAIL: Final = {
    ChannelEndOutcome.CLIENT_CLOSED: "the client closed its side",
    ChannelEndOutcome.SERVER_CLOSED: "the server closed the connection",
    ChannelEndOutcome.SERVER_STALLED: "the server read nothing for {deadline:g}s",
    ChannelEndOutcome.CLIENT_STALLED: "stdout was not drained for {deadline:g}s",
    ChannelEndOutcome.SERVER_FAILED: "the control socket failed",
    ChannelEndOutcome.CLIENT_FAILED: "stdin or stdout failed",
    ChannelEndOutcome.SERVER_UNRESPONSIVE: "the control socket sent nothing for {deadline:g}s",
}

_START_REFUSED_DETAIL: Final = "relay copy worker start was refused"


@dataclass(frozen=True)
class _Copy:
    """One direction whose I/O starts only after the shared startup decision."""

    source: ByteSource
    source_peer: Peer
    sink: ByteSink
    sink_peer: Peer


def _copy_after_start(
    gate: _StartupGate,
    state: _Relay,
    copy: _Copy,
) -> None:
    """Copy only after the owner has acquired both workers."""
    if gate.wait():
        state.copy(copy.source, copy.source_peer, copy.sink, copy.sink_peer)


def _finish_despite_interruptions[T](
    effect: Callable[[], T], interruptions: list[BaseException]
) -> T:
    """Retry ownership cleanup until it proves completion, retaining interruptions as notes."""
    while True:
        try:
            return effect()
        except BaseException as interruption:  # noqa: BLE001  # lint-waiver: LW-010255 [BLE001]; this ownership boundary must finish after every cancellation or signal before propagating the original startup failure; narrowing would let an unknown asynchronous exit strand a worker.
            interruptions.append(interruption)


def _run_copy_workers(
    *,
    connection: Connection,
    client: ClientStreams,
    threads: ReservableThreads,
    state: _Relay,
) -> ChannelEndOutcome | BridgeResult:
    """Acquire, commit, and supervise copy workers under one owner.

    A returned failure means every worker acquired before the refusal has observed the
    abort and settled without reading or writing either peer.
    """
    gate = state.startup_gate()
    upstream = _Copy(
        client.incoming,
        Peer.CLIENT,
        _ConnectionSink(connection),
        Peer.SERVER,
    )
    downstream = _Copy(
        _ConnectionSource(connection),
        Peer.SERVER,
        client.outgoing,
        Peer.CLIENT,
    )
    workers: list[WorkerReservation] = []
    try:
        # Acquire and positively start each reservation before asking for the next.
        # The gate keeps an eager target away from I/O, while this ordering makes a
        # closed refusal terminal: a failed process-dispatcher bootstrap cannot be
        # retried by the second reservation inside an already-doomed transaction.
        for copy, name in (
            (upstream, "relay-upstream"),
            (downstream, "relay-downstream"),
        ):
            worker = threads.reserve(
                lambda copy=copy: _copy_after_start(gate, state, copy),
                name=name,
            )
            workers.append(worker)
            worker.start()
        return state.supervise(gate)
    except BaseException as error:
        # An interrupt can land while acquiring a reservation, after a native
        # start takes effect but before it returns its handle, or during the
        # commit-to-supervision transition. Every acquired reservation is already
        # in ``workers``; an unobserved commit is converted to abort under the
        # supervisor's condition, so each possibly-started target is safe to settle.
        # A decision is committed here only after a worker has observed it, which
        # means ordinary relay supervision—not its startup transition—had begun.
        cleanup_interruptions: list[BaseException] = []
        committed = _finish_despite_interruptions(
            lambda: gate.decide(commit=False), cleanup_interruptions
        )
        if committed:
            raise
        for worker in workers:
            _finish_despite_interruptions(worker.settle, cleanup_interruptions)
        if cleanup_interruptions:
            error.add_note(
                "relay startup cleanup was interrupted "
                f"{len(cleanup_interruptions)} time(s) before it settled"
            )
        if isinstance(error, WorkerStartRefusedError):
            return BridgeResult(
                ChannelEndOutcome.CONNECT_FAILED,
                _START_REFUSED_DETAIL,
            )
        raise


def _deadline_seconds(outcome: ChannelEndOutcome, limits: BridgeLimits) -> float | None:
    """Which configured deadline decided ``outcome``, to name in its detail line.

    Every outcome not named here has a ``_DETAIL`` template with no ``{deadline}``
    placeholder, so the value returned for it is never actually formatted in.
    """
    if outcome is ChannelEndOutcome.SERVER_UNRESPONSIVE:
        return limits.read_deadline_seconds
    return limits.write_deadline_seconds


def connect_outcome(error: OSError) -> ChannelEndOutcome:
    """Classify a failed connect: the run is gone, access is denied, or the transport broke."""
    if isinstance(error, (ConnectionRefusedError, FileNotFoundError)):
        return ChannelEndOutcome.RUN_GONE
    if isinstance(error, PermissionError):
        return ChannelEndOutcome.CONNECT_DENIED
    return ChannelEndOutcome.CONNECT_FAILED


def dial(
    network: Dialer, address: str, limits: BridgeLimits | None = None
) -> Connection | BridgeResult:
    """Connect to ``address``, or the result that says why the relay cannot start."""
    limits = limits or BridgeLimits()
    try:
        return network.connect(address, timeout=limits.connect_timeout_seconds)
    except OSError as error:
        return BridgeResult(
            connect_outcome(error), f"connecting to {address}: {error.strerror or error}"
        )


def relay(
    connection: Connection,
    *,
    client: ClientStreams,
    threads: ReservableThreads,
    limits: BridgeLimits | None = None,
) -> BridgeResult:
    """Relay between ``client`` and an open ``connection``.

    Runs until one side ends, a write stalls, or the server goes silent past the read
    deadline. Returns once the outcome is decided and the connection is closed. A copy
    loop may still be blocked on the client's input or on a stalled output write; the
    caller ends the process.
    """
    limits = limits or BridgeLimits()
    state = _Relay(threads, limits)
    try:
        result = _run_copy_workers(
            connection=connection,
            client=client,
            threads=threads,
            state=state,
        )
    finally:
        connection.close()
    if isinstance(result, BridgeResult):
        return result
    outcome = result
    detail = _DETAIL[outcome].format(deadline=_deadline_seconds(outcome, limits))
    return BridgeResult(outcome, detail)


def run_bridge(
    *,
    network: Dialer,
    address: str,
    client: ClientStreams,
    threads: ReservableThreads,
    limits: BridgeLimits | None = None,
) -> BridgeResult:
    """Connect to ``address`` and relay (``relay``) until one side ends, stalls, or goes silent."""
    connection = dial(network, address, limits)
    if isinstance(connection, BridgeResult):
        return connection
    return relay(connection, client=client, threads=threads, limits=limits)
