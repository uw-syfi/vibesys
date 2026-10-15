"""An in-memory network: the same connections as :class:`~vs_sim.network.UnixNetwork`, no sockets.

All state sits behind one condition from the :class:`~vs_sim.concurrency.Threads` it is
built on, so under the cooperative simulator every blocking call (``accept``, ``recv``)
is a simulated wait that the seeded scheduler orders and the virtual clock times out.

A network can carry :class:`LinkFault` rules: a connection that drops from one side, a
reset, a one-way partition that heals or that ends the connection after a bound, and
delayed delivery. A rule fires on a counted send, never at a time, so a seed replays it.
"""

from __future__ import annotations

import errno
import math
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_sim.concurrency import Threads
    from vs_sim.network import Connection, Listener


class _Draws(Protocol):
    """A seeded stream, such as ``Sim.random``'s."""

    def randint(self, low: int, high: int, /) -> int: ...

    def random(self) -> float: ...


class End(StrEnum):
    """One end of a connection."""

    DIALER = "dialer"
    """The end ``connect`` returned."""
    ACCEPTOR = "acceptor"
    """The end ``accept`` returned."""


class LinkFaultKind(StrEnum):
    """What a :class:`LinkFault` does to its connection."""

    DROP = "drop"
    """``end`` is cut: its calls raise ``ConnectionAbortedError``; the peer reads the bytes
    already delivered, then EOF, and its sends raise ``BrokenPipeError``."""
    RESET = "reset"
    """Unread bytes in both directions are lost, and every call on either end raises
    ``ConnectionResetError``."""
    STALL = "stall"
    """A one-way partition that heals: bytes sent toward ``end`` are held for ``seconds``,
    then delivered in order."""
    BLACKHOLE = "blackhole"
    """A one-way partition that does not heal: bytes sent toward ``end`` vanish (still
    holding the sender's buffer, as unacknowledged bytes do), and ``seconds`` later the connection breaks, so every call on either end raises
    ``ConnectionAbortedError`` (``ETIMEDOUT``), as a stream whose retransmissions ran out."""
    DELAY = "delay"
    """Each chunk sent toward ``end`` from then on arrives ``seconds`` after it was sent."""


@dataclass(frozen=True)
class LinkFault:
    """A fault on one connection, fired just before a counted send on it."""

    kind: LinkFaultKind
    end: End = End.DIALER
    """The end that is cut (``DROP``) or that the bytes travel toward (one-way kinds)."""
    connection: int = 1
    """Which connection, by the order the network established them, from 1."""
    before_send: int = 1
    """The fault fires just before this send (from 1) on the connection, counting both ends."""
    seconds: float = 1.0
    """How long a ``STALL`` holds, a ``BLACKHOLE`` lasts before breaking, a ``DELAY`` adds."""

    @classmethod
    def draw(
        cls, rng: _Draws, *, connections: int = 1, sends: int = 8, seconds: float = 1.0
    ) -> LinkFault:
        """Any kind, either end, on one of the first ``connections`` before one of its first ``sends``.

        Its duration is drawn up to ``seconds``.
        """
        kinds = tuple(LinkFaultKind)
        ends = tuple(End)
        return cls(
            kind=kinds[rng.randint(0, len(kinds) - 1)],
            end=ends[rng.randint(0, len(ends) - 1)],
            connection=rng.randint(1, connections),
            before_send=rng.randint(1, sends),
            seconds=seconds * (0.5 + rng.random() / 2),
        )


@dataclass(frozen=True)
class FiredFault:
    """A :class:`LinkFault` that fired, and when (a ``threads.now()`` reading)."""

    fault: LinkFault
    at: float


class _Pipe:
    """Bytes one end sends and the other reads."""

    def __init__(self) -> None:
        self.data = bytearray()
        """Delivered and not yet read."""
        self.in_flight: deque[tuple[float, bytes]] = deque()
        """Sent, delivered at the time paired with each chunk, in order."""
        self.writer_closed = False
        self.reader_closed = False
        self.held_until: float | None = None
        self.delay = 0.0
        self.vanishes = False
        self.lost = 0
        """Bytes sent into a blackhole: they hold the sender's buffer, as unacknowledged bytes do."""

    def settle(self, now: float) -> None:
        """Deliver every in-flight chunk whose time has come."""
        while self.in_flight and self.in_flight[0][0] <= now:
            self.data += self.in_flight.popleft()[1]

    def carry(self, data: bytes, now: float) -> None:
        if self.vanishes:
            self.lost += len(data)
            return
        ready = now + self.delay
        if self.held_until is not None:
            ready = max(ready, self.held_until)
        if self.in_flight:
            ready = max(ready, self.in_flight[-1][0])
        if ready <= now:
            self.data += data
        else:
            self.in_flight.append((ready, data))

    def unread(self) -> int:
        return self.lost + len(self.data) + sum(len(chunk) for _, chunk in self.in_flight)

    def at_eof(self) -> bool:
        return self.writer_closed and not self.data and not self.in_flight


class _Link:
    """What the two ends of one connection share: their pipes, the send count, its faults."""

    def __init__(self, index: int, faults: Sequence[LinkFault]) -> None:
        self.index = index
        self.toward = {End.DIALER: _Pipe(), End.ACCEPTOR: _Pipe()}
        self.sends = 0
        self.faults = tuple(fault for fault in faults if fault.connection == index)
        self.reset = False
        self.breaks_at: float | None = None
        self.aborted: set[End] = set()

    def next_change(self) -> float | None:
        """The next time a waiter's predicate may turn true without any call."""
        times = [pipe.in_flight[0][0] for pipe in self.toward.values() if pipe.in_flight]
        if self.breaks_at is not None:
            times.append(self.breaks_at)
        return min(times, default=None)


class _SimConnection:
    def __init__(self, network: SimNetwork, link: _Link, end: End) -> None:
        self.network = network
        self._link = link
        self._end = end
        self._incoming = link.toward[end]
        self._outgoing = link.toward[End.ACCEPTOR if end is End.DIALER else End.DIALER]
        self._closed = False

    def send(self, data: bytes, timeout: float | None = None) -> None:
        network = self.network
        deadline = None if timeout is None else network.threads.now() + timeout
        pipe = self._outgoing
        view = bytes(data)
        with network.cond:
            self._link.sends += 1
            network.fire(self._link)
            while True:
                self._raise_if_unusable()
                if pipe.reader_closed:
                    raise BrokenPipeError(errno.EPIPE, "peer closed")
                room = network.room(pipe)
                if room > 0:
                    pipe.carry(view[:room], network.threads.now())
                    view = view[room:]
                    network.cond.notify_all()
                if not view:
                    return
                if not network.wait(
                    self._link,
                    lambda: self._unusable() or pipe.reader_closed or network.room(pipe) > 0,
                    deadline,
                ):
                    message = "no room to send"
                    raise TimeoutError(message)

    def recv(self, max_bytes: int, timeout: float | None = None) -> bytes:
        network = self.network
        deadline = None if timeout is None else network.threads.now() + timeout
        pipe = self._incoming
        with network.cond:
            self._raise_if_unusable()
            if not network.wait(
                self._link, lambda: bool(pipe.data) or pipe.at_eof() or self._unusable(), deadline
            ):
                message = "recv timed out"
                raise TimeoutError(message)
            self._raise_if_unusable()
            chunk = bytes(pipe.data[:max_bytes])
            del pipe.data[:max_bytes]
            # A sender may be waiting for the room this read made.
            network.cond.notify_all()
            return chunk

    def peer_closed(self) -> bool:
        with self.network.cond:
            if self._unusable():
                return True
            self._incoming.settle(self.network.threads.now())
            return self._incoming.at_eof()

    def close(self) -> None:
        with self.network.cond:
            if self._closed:
                return
            self._closed = True
            # Into a blackhole the end of stream vanishes like any byte.
            self._outgoing.writer_closed = not self._outgoing.vanishes
            self._incoming.reader_closed = True
            self.network.cond.notify_all()

    def _unusable(self) -> bool:
        link = self._link
        return (
            self._closed
            or self._end in link.aborted
            or link.reset
            or (link.breaks_at is not None and self.network.threads.now() >= link.breaks_at)
        )

    def _raise_if_unusable(self) -> None:
        link = self._link
        if self._closed:
            raise OSError(errno.EBADF, "connection closed")
        if self._end in link.aborted:
            raise ConnectionAbortedError(errno.ECONNABORTED, "connection dropped")
        if link.reset:
            raise ConnectionResetError(errno.ECONNRESET, "connection reset by peer")
        if link.breaks_at is not None and self.network.threads.now() >= link.breaks_at:
            raise ConnectionAbortedError(errno.ETIMEDOUT, "connection timed out")


class _SimListener:
    def __init__(self, network: SimNetwork, address: str) -> None:
        self.network = network
        self._address = address
        self.backlog: deque[_SimConnection] = deque()
        self.closed = False

    def accept(self, timeout: float | None = None) -> Connection:
        cond = self.network.cond
        with cond:
            if not cond.wait_for(lambda: bool(self.backlog) or self.closed, timeout):
                message = "accept timed out"
                raise TimeoutError(message)
            if self.closed:
                raise OSError(errno.EBADF, "listener closed")
            return self.backlog.popleft()

    def close(self) -> None:
        with self.network.cond:
            if self.closed:
                return
            self.closed = True
            self.network.listeners.pop(self._address, None)
            for waiting in self.backlog:
                waiting.close()
            self.network.cond.notify_all()


DEFAULT_BUFFER_BYTES = 256 << 10
"""Unread bytes one direction of a connection holds by default, about a Unix stream socket's."""


class SimNetwork:
    """Listeners and connections held in memory; addresses are any strings."""

    def __init__(
        self,
        threads: Threads,
        *,
        buffer_bytes: int | None = DEFAULT_BUFFER_BYTES,
        faults: Sequence[LinkFault] = (),
    ) -> None:
        """Build on ``threads``, whose condition makes blocking calls simulated waits.

        ``buffer_bytes`` bounds the unread bytes each direction of a connection holds,
        as a socket's buffer does: a ``send`` beyond it waits for the peer to read, so
        a peer that stops reading blocks its writer, as on a real socket. ``None``
        holds any amount, so a ``send`` never waits; only a test that is about
        something else should ask for that. ``faults`` are the :class:`LinkFault`
        rules this network fires; :attr:`fired` lists those that did.
        """
        if buffer_bytes is not None and buffer_bytes <= 0:
            message = f"buffer_bytes must be positive or None, not {buffer_bytes}"
            raise ValueError(message)
        self.threads = threads
        self.cond = threads.condition()
        self.listeners: dict[str, _SimListener] = {}
        self._buffer_bytes = buffer_bytes
        self._faults = tuple(faults)
        self._connections = 0
        self._fired: list[FiredFault] = []

    @property
    def fired(self) -> tuple[FiredFault, ...]:
        """The faults that fired so far, in order."""
        with self.cond:
            return tuple(self._fired)

    def room(self, pipe: _Pipe) -> int:
        """How many more bytes ``pipe`` holds unread."""
        if self._buffer_bytes is None:
            return 1 << 62
        return max(self._buffer_bytes - pipe.unread(), 0)

    def fire(self, link: _Link) -> None:
        """Apply the faults due before ``link``'s latest send. Holds :attr:`cond`."""
        now = self.threads.now()
        for fault in link.faults:
            if fault.before_send != link.sends:
                continue
            self._fired.append(FiredFault(fault, now))
            pipe = link.toward[fault.end]
            if fault.kind is LinkFaultKind.DROP:
                link.aborted.add(fault.end)
                link.toward[
                    End.ACCEPTOR if fault.end is End.DIALER else End.DIALER
                ].writer_closed = True
                pipe.reader_closed = True
            elif fault.kind is LinkFaultKind.RESET:
                link.reset = True
                for each in link.toward.values():
                    each.data.clear()
                    each.in_flight.clear()
            elif fault.kind is LinkFaultKind.STALL:
                pipe.held_until = now + fault.seconds
            elif fault.kind is LinkFaultKind.BLACKHOLE:
                pipe.vanishes = True
                pipe.in_flight.clear()
                link.breaks_at = now + fault.seconds
            else:
                pipe.delay = fault.seconds
        self.cond.notify_all()

    def wait(self, link: _Link, ready: Callable[[], bool], deadline: float | None) -> bool:
        """Wait under :attr:`cond` until ``ready``, delivering ``link``'s bytes as they land.

        False once ``deadline`` (a ``threads.now()`` reading) passed first.
        """
        while True:
            now = self.threads.now()
            for pipe in link.toward.values():
                pipe.settle(now)
            if ready():
                return True
            remaining = math.inf if deadline is None else deadline - now
            if remaining <= 0:
                return False
            change = link.next_change()
            bound = min(remaining, math.inf if change is None else max(change - now, 0.0))
            # Not ``wait_for``: a send that put bytes in flight must wake this loop to time them.
            self.cond.wait(None if math.isinf(bound) else bound)

    def listen(self, address: str) -> Listener:
        """Bind ``address``; ``OSError`` (address in use) when a listener holds it."""
        with self.cond:
            if address in self.listeners:
                raise OSError(errno.EADDRINUSE, "address in use", address)
            listener = _SimListener(self, address)
            self.listeners[address] = listener
            return listener

    def connect(self, address: str, timeout: float | None = None) -> Connection:  # noqa: ARG002  # LW-163921 [ARG002]; a connect completes at once in memory, and the parameter is the Network contract.
        """Dial ``address``; the server end waits in the listener's backlog until accepted."""
        with self.cond:
            listener = self.listeners.get(address)
            if listener is None:
                message = f"nothing listens on {address}"
                raise ConnectionRefusedError(message)
            self._connections += 1
            link = _Link(self._connections, self._faults)
            listener.backlog.append(_SimConnection(self, link, End.ACCEPTOR))
            self.cond.notify_all()
            return _SimConnection(self, link, End.DIALER)
