"""The queue status wire: one request line to the host, one ``QueueReading`` line back.

The agent's tool server runs where the agent runs (often a container without the
cluster's tools or credentials), so it cannot read the queue itself. The host serves
the run's ``QueueStatusReader`` on a unix socket in the run's private socket directory,
and the tool server reads through ``SocketQueueStatusReader``, which is itself a
reader: every transport failure becomes a ``QueueStatusUnavailable``.

The wire carries no token: the request has no arguments and names nothing, the reply
is the same for every agent of the run, and the socket's directory is private to the
run's user.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from vs_decode.api import Unreadable, decode_json
from vs_evaluation.queue_status import QUEUE_READING, QueueStatusUnavailable
from vs_evaluation.socket_lines import read_line

if TYPE_CHECKING:
    from pathlib import Path

    from vs_evaluation.queue_status import QueueStatus, QueueStatusReader
    from vs_sim.api import Clock, Connection, Listener, Network, Threads

CALL_TIMEOUT_S = 90.0
"""How long the tool waits for the host's answer.

The host answers from a reading at most ``fresh_for_s`` old, or reads the backend once,
which its own transport bounds (a Slurm reader allows 30 s per attempt and retries once).
This leaves room for that and stays well inside an agent CLI's tool-call timeout.
"""
REQUEST_READ_S = 10.0
"""How long the host waits for a connected client's request line before hanging up."""
_MAX_REQUEST_BYTES = 4096
MAX_REPLY_BYTES = 1_048_576
"""The largest reply line the tool reads; every reading must fit in it."""


class QueueStatusCall(BaseModel):
    """The one request the wire carries."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action: Literal["read"] = "read"


class SocketQueueStatusReader:
    """Reads the host's queue status over its unix socket; a failed call is unavailable."""

    def __init__(
        self, path: Path, *, network: Network, clock: Clock, timeout_s: float = CALL_TIMEOUT_S
    ) -> None:
        """Bind the socket path, the network that dials it and the clock its deadline runs on."""
        self._path = path
        self._network = network
        self._clock = clock
        self._timeout_s = timeout_s

    def read(self) -> QueueStatus | QueueStatusUnavailable:
        """One call to the host, bounded by the call timeout."""
        deadline = self._clock.now() + self._timeout_s
        try:
            connection = self._network.connect(str(self._path), self._timeout_s)
            try:
                connection.send(
                    QueueStatusCall().model_dump_json().encode() + b"\n", self._timeout_s
                )
                line = read_line(connection, self._clock, deadline, MAX_REPLY_BYTES)
            finally:
                connection.close()
        except OSError as error:
            return QueueStatusUnavailable.because(
                f"queue status service unreachable: {type(error).__name__}: {error}"
            )
        if line is None:
            return QueueStatusUnavailable.because("queue status service closed without a reply")
        reading = decode_json(
            line, QUEUE_READING, source="queue status reply", max_bytes=MAX_REPLY_BYTES
        )
        if isinstance(reading, Unreadable):
            return QueueStatusUnavailable.because(
                f"queue status service sent a malformed reply ({len(reading.problems)} problem(s))"
            )
        return reading


class QueueStatusService:
    """Serves one reader on a unix socket, one short call per connection, until closed.

    The caller creates the socket's directory private; the service never creates it.
    Each connection is answered on its own worker thread, so a slow backend read delays
    only its callers. Wrap the reader in ``CachedQueueStatusReader`` so many agents cost
    one backend read.
    """

    def __init__(
        self,
        reader: QueueStatusReader,
        socket_path: Path,
        *,
        network: Network,
        threads: Threads,
        clock: Clock,
    ) -> None:
        """Bind the reader to serve, where to listen, and the effects the service uses."""
        self._reader = reader
        self._socket_path = socket_path
        self._network = network
        self._threads = threads
        self._clock = clock
        self._guard = threads.lock()
        self._listener: Listener | None = None
        self._open: set[Connection] = set()
        self._closing = False

    @property
    def socket_path(self) -> Path:
        """Where the service listens."""
        return self._socket_path

    def start(self) -> None:
        """Listen, replacing a stale socket a previous process of this run left behind."""
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()
        listener = self._network.listen(str(self._socket_path))
        self._listener = listener
        self._threads.spawn(lambda: self._accept_loop(listener), name="queue-status-service")

    def close(self) -> None:
        """Stop accepting, hang up on open calls and remove the socket; safe to repeat."""
        with self._guard:
            self._closing = True
            listener, self._listener = self._listener, None
            open_connections, self._open = self._open, set()
        if listener is not None:
            listener.close()
        for connection in open_connections:
            connection.close()
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()

    def _accept_loop(self, listener: Listener) -> None:
        while True:
            try:
                connection = listener.accept()
            except OSError:
                return  # the listener was closed
            with self._guard:
                closing = self._closing
                if not closing:
                    self._open.add(connection)
            if closing:
                connection.close()
                return
            self._threads.spawn(lambda c=connection: self._answer(c), name="queue-status-call")

    def _answer(self, connection: Connection) -> None:
        try:
            line = read_line(
                connection, self._clock, self._clock.now() + REQUEST_READ_S, _MAX_REQUEST_BYTES
            )
            if line is None:
                return
            call = decode_json(
                line, QueueStatusCall, source="queue status call", max_bytes=_MAX_REQUEST_BYTES
            )
            reading = (
                QueueStatusUnavailable.because("malformed queue status call")
                if isinstance(call, Unreadable)
                else self._reader.read()
            )
            connection.send(reading.model_dump_json().encode() + b"\n", REQUEST_READ_S)
        except OSError:
            pass  # the client went away or never sent a whole request
        finally:
            with self._guard:
                self._open.discard(connection)
            connection.close()


__all__ = [
    "CALL_TIMEOUT_S",
    "QueueStatusCall",
    "QueueStatusService",
    "SocketQueueStatusReader",
]
