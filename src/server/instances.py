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
    from vs_project.api import OperatorDialer
    from vs_sim.api import Clock, Connection

CONTROL_REPLY_TIMEOUT_SECONDS = 5.0
"""How long the complete connect, send, and acknowledgment exchange may take."""
_MAX_REPLY_BYTES = 1 << 20


class ControlSocketStopRequester:
    """Send ``command.stop`` over the server's JSONL control socket.

    This is the same command an attached client sends, so the run stops at its
    next controlled boundary with its state persisted, exactly as a stop from
    the client does. The socket serves only a connection that presents the operator key,
    which ``network`` (an ``OperatorDialer``) does on every connect.
    """

    def __init__(
        self,
        network: OperatorDialer,
        *,
        clock: Clock,
        deadline_clock: Clock,
        timeout: float = CONTROL_REPLY_TIMEOUT_SECONDS,
    ) -> None:
        """Dial through ``network``; bound the whole request by ``timeout``.

        ``clock`` counts seconds since the epoch and stamps the stop request;
        ``deadline_clock`` is monotonic and measures the request duration.
        """
        self._network = network
        self._clock = clock
        self._deadline_clock = deadline_clock
        self._timeout = timeout

    def request_stop(self, socket_path: str) -> bool:
        """Send the stop and read one response line within one call deadline."""
        deadline = self._deadline_clock.now() + self._timeout
        try:
            connection = self._network.connect(socket_path, self._remaining(deadline))
        except OSError:
            return False
        try:
            connection.send(
                StopCommand(timestamp=utc_datetime(self._clock)).model_dump_json().encode() + b"\n",
                self._remaining(deadline),
            )
            line = _read_line(connection, self._deadline_clock, deadline)
        except OSError:
            return False
        finally:
            connection.close()
        response = decode_json(
            line, ResponseEnvelopeReader, source="stop reply", max_bytes=_MAX_REPLY_BYTES
        )
        if isinstance(response, Unreadable):
            return False
        return response.ok and response.ack is not None and response.ack.action == "pause_now"

    def _remaining(self, deadline: float) -> float:
        """Return time left on ``deadline``; fail once the whole call is out of time."""
        remaining = deadline - self._deadline_clock.now()
        if remaining <= 0:
            message = "no complete stop reply before the request deadline"
            raise TimeoutError(message)
        return remaining


def _read_line(connection: Connection, clock: Clock, deadline: float) -> bytes:
    """Read through the first newline, or to EOF, within ``deadline``."""
    buffer = b""
    while b"\n" not in buffer:
        if len(buffer) > _MAX_REPLY_BYTES:
            break
        remaining = deadline - clock.now()
        if remaining <= 0:
            message = "no complete stop reply before the request deadline"
            raise TimeoutError(message)
        chunk = connection.recv(4096, remaining)
        if not chunk:
            break
        buffer += chunk
    return buffer.split(b"\n", 1)[0]
