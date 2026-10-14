"""An in-memory transport for driving a ``SkyPilotBridge`` without a real socket.

The bridge takes its ``Network`` and ``Threads`` as seams. A test builds one
``MemoryTransport``, passes ``transport.network`` and ``transport.threads`` to the
bridge, and talks to it through ``transport.dial(bridge)``. Nothing touches the
filesystem, so no path-length limit applies and no test depends on the host's
temporary directory.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, BinaryIO, cast

from vs_sandbox.api.skypilot import AckedFrame, AckRequest, decode_response, encode_message
from vs_sim.api import OsThreads
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sandbox.api.skypilot import EvaluationRequest, ResponseFrame, SkyPilotBridge
    from vs_sim.api import Connection


class BridgeClient:
    """One client connection: send a request, read newline-framed response frames."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def send(self, payload: bytes) -> None:
        """Write raw bytes to the bridge."""
        self._connection.send(payload)

    def readline(self) -> bytes:
        """One newline-terminated line, or what arrived before the bridge closed."""
        line = b""
        while not line.endswith(b"\n"):
            chunk = self._connection.recv(1, HANG_GUARD_S)
            if not chunk:
                break
            line += chunk
        return line

    def read_frame(self) -> ResponseFrame:
        """The next decoded response frame."""
        return decode_response(self.readline())

    def close(self) -> None:
        """Close this end."""
        self._connection.close()


class MemoryTransport:
    """The network and thread seams a bridge under test is built with."""

    def __init__(self) -> None:
        self.threads = OsThreads()
        self.network = SimNetwork(self.threads)

    def dial(self, bridge: SkyPilotBridge) -> BridgeClient:
        """Connect to a started bridge."""
        return BridgeClient(self.network.connect(str(bridge.socket_path), HANG_GUARD_S))

    def evaluate(self, bridge: SkyPilotBridge, request: EvaluationRequest) -> list[ResponseFrame]:
        """Run one request to its terminal frame, acknowledging a result."""
        client = self.dial(bridge)
        try:
            client.send(encode_message(request))
            frames: list[ResponseFrame] = []
            while True:
                frame = client.read_frame()
                frames.append(frame)
                if frame.type in {"result", "error"}:
                    break
            if frames[-1].type == "result":
                client.send(encode_message(AckRequest(invocation_id=request.invocation_id)))
                assert client.read_frame().type == "acked"
        finally:
            client.close()
        return frames


class _ConnectionReader:
    """A buffered ``readline`` over a vs_sim connection, shaped like ``socket.makefile("rb")``."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._buffer = bytearray()

    def readline(self, limit: int = -1) -> bytes:
        while b"\n" not in self._buffer and (limit < 0 or len(self._buffer) < limit):
            chunk = self._connection.recv(65536, HANG_GUARD_S)
            if not chunk:
                break
            self._buffer += chunk
        end = self._buffer.find(b"\n") + 1 or len(self._buffer)
        if limit >= 0:
            end = min(end, limit)
        line = bytes(self._buffer[:end])
        del self._buffer[:end]
        return line


class MemoryPeer:
    """The evaluator helper's view of an in-memory connection (its ``BridgePeer``)."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def sendall(self, data: bytes, /) -> None:
        self._connection.send(data)

    def makefile(self, mode: str, /) -> BinaryIO:
        assert mode == "rb"
        return cast("BinaryIO", _ConnectionReader(self._connection))

    def close(self) -> None:
        self._connection.close()


def _is_result(frame: bytes) -> bool:
    try:
        decoded = json.loads(frame)
    except ValueError:
        return False
    return isinstance(decoded, dict) and decoded.get("type") == "result"


class ScriptedBridge:
    """A bridge that answers one evaluator-helper conversation from a script.

    It listens on ``address`` of a ``MemoryTransport``, reads the request line,
    sends each frame, and, when one is a result, reads the acknowledgement and
    answers it. ``requests`` collects every decoded request, ``acks`` every
    decoded acknowledgement.
    """

    def __init__(
        self,
        transport: MemoryTransport,
        address: str,
        frames: list[bytes],
        *,
        acknowledge: bool = True,
        ack_reply: bytes | None = None,
    ) -> None:
        self.requests: list[dict[str, object]] = []
        self.acks: list[dict[str, object]] = []
        self._listener = transport.network.listen(address)
        self._transport = transport
        self._frames = frames
        self._acknowledge = acknowledge
        self._ack_reply = ack_reply
        self._address = address
        self._worker = transport.threads.spawn(self._serve, name="scripted-bridge")

    def dial(self, socket_path: Path) -> MemoryPeer:
        """Connect the way ``run_evaluator`` dials: by the socket path it was given."""
        assert str(socket_path) == self._address
        return MemoryPeer(self._transport.network.connect(self._address, HANG_GUARD_S))

    def finish(self) -> None:
        """Wait for the scripted conversation to finish."""
        self._worker.join(HANG_GUARD_S)

    def _serve(self) -> None:
        connection = self._listener.accept(HANG_GUARD_S)
        reader = _ConnectionReader(connection)
        try:
            self.requests.append(json.loads(reader.readline()))
            for frame in self._frames:
                connection.send(frame)
            if self._acknowledge and any(_is_result(frame) for frame in self._frames):
                ack = json.loads(reader.readline())
                self.acks.append(ack)
                connection.send(
                    self._ack_reply
                    or encode_message(AckedFrame(invocation_id=cast("str", ack["invocation_id"])))
                )
        finally:
            connection.close()
            self._listener.close()
