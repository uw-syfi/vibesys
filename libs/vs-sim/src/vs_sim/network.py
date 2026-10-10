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
import socket
from pathlib import Path
from typing import Protocol


class Connection(Protocol):
    """One end of an established stream."""

    def send(self, data: bytes) -> None:
        """Write all of ``data``; ``BrokenPipeError`` when the peer has closed its end."""
        ...

    def recv(self, max_bytes: int, timeout: float | None = None) -> bytes:
        """Up to ``max_bytes`` bytes, at least one; ``b""`` once the peer closed and all data was read.

        Raises:
            TimeoutError: nothing arrived within ``timeout`` seconds (``None`` waits for ever).
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


class _UnixConnection:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def recv(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self._sock.settimeout(timeout)
        return self._sock.recv(max_bytes)

    def peer_closed(self) -> bool:
        try:
            return self._sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def close(self) -> None:
        with contextlib.suppress(OSError):
            # Sends the peer its EOF and wakes a thread blocked in recv, even though the
            # descriptor stays open until that thread returns.
            self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()


class _UnixListener:
    def __init__(self, sock: socket.socket, path: Path) -> None:
        self._sock = sock
        self._path = path
        info = path.stat()
        self._identity = (info.st_dev, info.st_ino)

    def accept(self, timeout: float | None = None) -> Connection:
        self._sock.settimeout(timeout)
        client, _ = self._sock.accept()
        client.settimeout(None)
        return _UnixConnection(client)

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

    def listen(self, address: str) -> Listener:
        """Bind and listen on the path; ``OSError`` when a socket file is already there."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(address)
            sock.listen()
        except OSError:
            sock.close()
            raise
        return _UnixListener(sock, Path(address))

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
        sock.settimeout(None)
        return _UnixConnection(sock)
