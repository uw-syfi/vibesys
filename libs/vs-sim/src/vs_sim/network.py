"""Local stream connections, addressed by a Unix socket path, behind an interface a simulator can drive.

Product code that serves or dials a local socket takes a :class:`Network`. Production
wiring passes :class:`UnixNetwork` (``AF_UNIX`` stream sockets); a test passes an
in-memory network from :mod:`vs_sim.api.testing` whose blocking calls are driven by the
simulator's threads, so a server and its clients run deterministically in one process.

The interface moves bytes only. Framing (JSONL, length prefixes) belongs to the caller,
and so does the choice of thread per connection: a server written against this interface
runs its own accept loop on a :class:`~vs_sim.concurrency.Threads` worker.
"""

from __future__ import annotations

import contextlib
import errno
import math
import select
import socket
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vs_sim.clock import MonotonicClock

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_sim.clock import Clock


class Connection(Protocol):
    """One end of an established stream."""

    def send(self, data: bytes, timeout: float | None = None) -> None:
        """Write all of ``data``; ``BrokenPipeError`` when the peer has closed its end.

        Raises:
            TimeoutError: a peer that stopped reading left no room for all of ``data``
                within ``timeout`` seconds (``None`` waits for ever). Part of it may
                have been written.
            ConnectionError: the connection broke (reset, aborted, timed out).
            OSError: this end was closed.
        """
        ...

    def recv(self, max_bytes: int, timeout: float | None = None) -> bytes:
        """Up to ``max_bytes`` bytes, at least one; ``b""`` once the peer closed and all data was read.

        Raises:
            TimeoutError: nothing arrived within ``timeout`` seconds (``None`` waits for ever).
            ConnectionError: the connection broke (reset, aborted, timed out).
            OSError: this end was closed.
        """
        ...

    def peer_closed(self) -> bool:
        """Without blocking: whether the peer closed and nothing is left to read."""
        ...

    def close(self) -> None:
        """Close this end; the peer reads EOF after the data already sent. Closing twice is fine."""
        ...


class Listener(Protocol):
    """A bound address clients can connect to."""

    def accept(self, timeout: float | None = None) -> Connection:
        """The next client connection.

        Raises:
            TimeoutError: no client connected within ``timeout`` seconds.
            OSError: the listener is closed.
        """
        ...

    def close(self) -> None:
        """Stop listening and free the address. Closing twice is fine."""
        ...


class Network(Protocol):
    """Where listeners are bound and clients dial."""

    def listen(self, address: str) -> Listener:
        """Bind ``address``; ``OSError`` when it is in use."""
        ...

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        """Dial ``address``; ``ConnectionRefusedError`` when nothing listens there."""
        ...


class DeadlineNetwork(Network, Protocol):
    """A network and the monotonic timeline that its blocking timeouts use.

    A caller can turn one duration into a deadline, then pass the decreasing remainder
    to several network calls without combining clocks from different implementations.
    """

    def now(self) -> float:
        """Seconds on the same monotonic timeline as this network's timeouts."""
        ...


class _UnixConnection:
    """One stream socket, held non-blocking: every wait is a ``poll`` bounded by a deadline.

    A blocking socket cannot bound a send. ``MSG_DONTWAIT`` makes one call non-blocking on
    Linux but not on macOS, which blocks until the peer reads, and a socket timeout is
    per socket, so it would also bound a ``recv`` or ``send`` made from another thread. A
    non-blocking socket never blocks in ``send`` or ``recv``: they return what fits or
    what is there, or raise ``BlockingIOError``, on every host. Waiting for room or for
    bytes is then ``poll`` with the time left to this call's own deadline.

    ``poll`` does not wake when another thread closes the descriptor it waits on (macOS
    never wakes it, even after a ``shutdown``). So ``close`` shuts the stream down, which
    wakes every waiter, and the descriptor is closed by the last call still using it.
    """

    def __init__(self, sock: socket.socket, clock: Clock) -> None:
        sock.settimeout(0.0)  # Non-blocking: a call returns at once or raises BlockingIOError.
        self._sock = sock
        self._clock = clock
        self._lock = threading.Lock()
        self._calls_in_flight = 0
        self._closing = False

    def send(self, data: bytes, timeout: float | None = None) -> None:
        deadline = None if timeout is None else self._clock.now() + timeout
        view = memoryview(data)
        with self._in_use():
            while view:
                view = view[self._send_now(view) :]
                if view and not self._wait(select.POLLOUT, deadline):
                    message = f"no room to send within {timeout} s"
                    raise TimeoutError(message)

    def recv(self, max_bytes: int, timeout: float | None = None) -> bytes:
        deadline = None if timeout is None else self._clock.now() + timeout
        with self._in_use():
            while (chunk := self._recv_now(max_bytes)) is None:
                if not self._wait(select.POLLIN, deadline):
                    message = f"nothing arrived within {timeout} s"
                    raise TimeoutError(message)
            return chunk

    def _send_now(self, view: memoryview) -> int:
        """How many bytes the socket took without waiting; 0 when its buffer is full."""
        try:
            return self._sock.send(view)
        except BlockingIOError:
            return 0

    def _recv_now(self, max_bytes: int) -> bytes | None:
        """What is there to read without waiting; ``None`` when nothing has arrived."""
        try:
            return self._sock.recv(max_bytes)
        except BlockingIOError:
            return None

    def _wait(self, event: int, deadline: float | None) -> bool:
        """Whether ``event`` (or a hang-up or error) arrived before ``deadline``; ``None`` waits for ever."""
        poller = select.poll()
        poller.register(self._sock, event)
        if deadline is None:
            return bool(poller.poll())
        remaining = deadline - self._clock.now()
        return bool(poller.poll(max(math.ceil(remaining * 1000), 0)))

    def peer_closed(self) -> bool:
        try:
            with self._in_use():
                return self._sock.recv(1, socket.MSG_PEEK) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            with contextlib.suppress(OSError):
                # Sends the peer its EOF and wakes every thread waiting on this end.
                self._sock.shutdown(socket.SHUT_RDWR)
            if self._calls_in_flight == 0:
                self._sock.close()

    @contextlib.contextmanager
    def _in_use(self) -> Iterator[None]:
        """Keep the descriptor open for one call; an end already closed raises ``OSError``."""
        with self._lock:
            if self._closing:
                raise OSError(errno.EBADF, "connection closed")
            self._calls_in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self._calls_in_flight -= 1
                if self._closing and self._calls_in_flight == 0:
                    self._sock.close()


class _UnixListener:
    def __init__(self, sock: socket.socket, path: Path, clock: Clock) -> None:
        self._sock = sock
        self._path = path
        self._clock = clock
        info = path.stat()
        self._identity = (info.st_dev, info.st_ino)

    def accept(self, timeout: float | None = None) -> Connection:
        self._sock.settimeout(timeout)
        client, _ = self._sock.accept()
        return _UnixConnection(client, self._clock)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            # Wakes a thread blocked in accept on Linux before the descriptor goes away.
            self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()
        self._unlink_if_ours()

    def _unlink_if_ours(self) -> None:
        # A later listener may have replaced the file at this path; only remove the one we bound.
        try:
            info = self._path.stat()
        except FileNotFoundError:
            return
        if (info.st_dev, info.st_ino) == self._identity:
            self._path.unlink(missing_ok=True)


class UnixNetwork:
    """``AF_UNIX`` stream sockets; an address is a filesystem path."""

    def __init__(self) -> None:
        """A network whose timeouts all run on one monotonic clock."""
        self._clock: Clock = MonotonicClock()

    def now(self) -> float:
        """Seconds on the monotonic timeline used to bound socket operations."""
        return self._clock.now()

    def listen(self, address: str) -> Listener:
        """Bind and listen on the path; ``OSError`` when a socket file is already there."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(address)
            sock.listen()
        except OSError:
            sock.close()
            raise
        return _UnixListener(sock, Path(address), self._clock)

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        """Dial the path; a missing or unbound path is ``ConnectionRefusedError``."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(address)
        except FileNotFoundError:
            sock.close()
            message = f"nothing listens on {address}"
            raise ConnectionRefusedError(message) from None
        except OSError:
            sock.close()
            raise
        return _UnixConnection(sock, self._clock)
