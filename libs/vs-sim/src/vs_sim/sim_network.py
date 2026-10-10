"""An in-memory network: the same connections as :class:`~vs_sim.network.UnixNetwork`, no sockets.

All state sits behind one condition from the :class:`~vs_sim.concurrency.Threads` it is
built on, so under the cooperative simulator every blocking call (``accept``, ``recv``)
is a simulated wait that the seeded scheduler orders and the virtual clock times out.
"""

from __future__ import annotations

import errno
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_sim.concurrency import Threads
    from vs_sim.network import Connection, Listener


class _Pipe:
    """Bytes one end sends and the other reads."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.writer_closed = False
        self.reader_closed = False


class _SimConnection:
    def __init__(self, network: SimNetwork, incoming: _Pipe, outgoing: _Pipe) -> None:
        self.network = network
        self._incoming = incoming
        self._outgoing = outgoing
        self._closed = False

    def send(self, data: bytes) -> None:
        with self.network.cond:
            if self._closed:
                raise OSError(errno.EBADF, "connection closed")
            if self._outgoing.reader_closed:
                raise BrokenPipeError(errno.EPIPE, "peer closed")
            self._outgoing.data += data
            self.network.cond.notify_all()

    def recv(self, max_bytes: int, timeout: float | None = None) -> bytes:
        cond = self.network.cond
        with cond:
            if self._closed:
                raise OSError(errno.EBADF, "connection closed")
            pipe = self._incoming
            if not cond.wait_for(
                lambda: bool(pipe.data) or pipe.writer_closed or self._closed, timeout
            ):
                message = "recv timed out"
                raise TimeoutError(message)
            if self._closed:
                raise OSError(errno.EBADF, "connection closed")
            chunk = bytes(pipe.data[:max_bytes])
            del pipe.data[:max_bytes]
            return chunk

    def peer_closed(self) -> bool:
        with self.network.cond:
            return self._closed or (self._incoming.writer_closed and not self._incoming.data)

    def close(self) -> None:
        with self.network.cond:
            if self._closed:
                return
            self._closed = True
            self._outgoing.writer_closed = True
            self._incoming.reader_closed = True
            self.network.cond.notify_all()


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


class SimNetwork:
    """Listeners and connections held in memory; addresses are any strings."""

    def __init__(self, threads: Threads) -> None:
        """Build on ``threads``, whose condition makes blocking calls simulated waits."""
        self.cond = threads.condition()
        self.listeners: dict[str, _SimListener] = {}

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
            there, back = _Pipe(), _Pipe()
            client = _SimConnection(self, incoming=back, outgoing=there)
            server = _SimConnection(self, incoming=there, outgoing=back)
            listener.backlog.append(server)
            self.cond.notify_all()
            return client
