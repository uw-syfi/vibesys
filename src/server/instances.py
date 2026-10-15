"""Ask a live detached server to stop over its control socket.

The registry itself (records, locks, listing, the stop decision) is
``server.registry``. This requester speaks only the run protocol's control
envelope (``server.control_envelope``), never the payload types that import the
run engine, so ``vibesys instances stop`` stays as light as ``instances list``.

Public surface: ``ControlSocketStopRequester`` and
``CONTROL_REPLY_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.control_envelope import ResponseEnvelopeReader, StopCommand
from vs_decode.api import Unreadable, decode_json
from vs_sim.api import utc_datetime

if TYPE_CHECKING:
    from vs_sim.api import Clock, Connection, Network

CONTROL_REPLY_TIMEOUT_SECONDS = 5.0
"""How long a stop request waits to connect and for the server's acknowledgment."""
_MAX_REPLY_BYTES = 1 << 20


class ControlSocketStopRequester:
    """Send ``command.stop`` over the server's JSONL control socket.

    This is the same command an attached client sends, so the run stops at its
    next controlled boundary with its state persisted, exactly as a stop from
    the TUI does. Only the socket's owner can dial it: the socket is ``0600``
    inside the ``0700`` registry root.
    """

    def __init__(
        self, network: Network, *, clock: Clock, timeout: float = CONTROL_REPLY_TIMEOUT_SECONDS
    ) -> None:
        """Dial through ``network``; wait at most ``timeout`` to connect and for the reply.

        ``clock`` counts seconds since the epoch and stamps the stop request.
        """
        self._network = network
        self._clock = clock
        self._timeout = timeout

    def request_stop(self, socket_path: str) -> bool:
        """Send the stop and read one response line."""
        try:
            connection = self._network.connect(socket_path, self._timeout)
        except OSError:
            return False
        try:
            connection.send(
                StopCommand(timestamp=utc_datetime(self._clock)).model_dump_json().encode() + b"\n",
                self._timeout,
            )
            line = _read_line(connection, self._timeout)
        except OSError:
            return False
        finally:
            connection.close()
        response = decode_json(
            line, ResponseEnvelopeReader, source="stop reply", max_bytes=_MAX_REPLY_BYTES
        )
        if isinstance(response, Unreadable):
            return False
        return response.ok and response.ack is not None and response.ack.action == "stop"


def _read_line(connection: Connection, timeout: float) -> bytes:
    """Read through the first newline, or to EOF; ``TimeoutError`` if the peer stalls."""
    buffer = b""
    while b"\n" not in buffer:
        if len(buffer) > _MAX_REPLY_BYTES:
            break
        chunk = connection.recv(4096, timeout)
        if not chunk:
            break
        buffer += chunk
    return buffer.split(b"\n", 1)[0]
