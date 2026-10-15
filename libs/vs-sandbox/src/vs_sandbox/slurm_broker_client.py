"""Confined client for the host-owned Slurm process broker.

``BrokeredProcesses`` is the ``vs_sim`` ``Processes`` that ``vs_slurm``'s transport starts its
programs with inside a confined environment: a broker nobody serves is ``FileNotFoundError``,
a call the broker does not answer in time is ``ProcessTimedOut``, and a refusal is
``PermissionError``. A reply outside the broker protocol is ``BrokerReplyError``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vs_decode.api import Unreadable, decode_json
from vs_sandbox.slurm_broker_protocol import (
    BROKER_REPLY,
    MAX_FRAME_BYTES,
    BrokerRefused,
    ProcessCall,
)
from vs_sim.api import ProcessOutcome, ProcessTimedOut
from vs_slurm.api import MAX_TRANSPORT_STREAM_BYTES

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import Connection, Network, ProcessRun, ProcessSpec

_RECV_BYTES = 65_536


class BrokerReplyError(ConnectionError):
    """The broker hung up without a reply, or replied outside its protocol."""

    @classmethod
    def no_reply(cls) -> BrokerReplyError:
        """The broker closed the connection before a complete reply."""
        return cls("Slurm broker closed the connection without a reply")

    @classmethod
    def too_large(cls) -> BrokerReplyError:
        """The reply outgrew the frame bound."""
        return cls(f"Slurm broker reply is longer than {MAX_FRAME_BYTES} bytes")

    @classmethod
    def malformed(cls, reply: Unreadable) -> BrokerReplyError:
        """The reply line is not a broker reply."""
        return cls(f"Slurm broker reply is malformed: {len(reply.problems)} errors")


@dataclass(frozen=True)
class BrokeredProcesses:
    """Runs each transport program through the host broker's constrained capability.

    A run the broker does not answer within its timeout is :class:`ProcessTimedOut`; each
    wait on the broker (to connect, to send, for each part of the reply) is bounded by it.
    The broker runs the program, so it owns the program's process group.

    Raises from :meth:`run`:
        FileNotFoundError: nothing listens on ``socket_path``.
        PermissionError: the broker refused the call or could not run it.
        BrokerReplyError: the broker hung up early or replied outside its protocol.
    """

    socket_path: Path
    token: str = field(repr=False)
    network: Network

    def run(self, spec: ProcessSpec, *, timeout: float | None) -> ProcessRun:
        """Ask the broker to run ``spec.argv`` with ``spec.input`` as its stdin."""
        if timeout is None:
            message = "a brokered Slurm transport call needs a timeout"
            raise ValueError(message)
        if spec.cwd is not None or spec.env is not None:
            message = "the Slurm broker runs a program in its own directory and environment"
            raise ValueError(message)
        request = ProcessCall.model_construct(
            token=self.token,
            argv=spec.argv,
            stdin=spec.input.decode() if spec.input else None,
            timeout=timeout,
            max_output_bytes=min(spec.max_output_bytes, MAX_TRANSPORT_STREAM_BYTES),
        )
        try:
            line = _exchange(
                self.network,
                str(self.socket_path),
                request.model_dump_json().encode() + b"\n",
                timeout,
            )
        except ConnectionRefusedError as error:
            raise FileNotFoundError(str(error)) from error
        except TimeoutError:
            return ProcessTimedOut(timeout, b"", b"")
        reply = decode_json(
            line, BROKER_REPLY, source="Slurm broker reply", max_bytes=MAX_FRAME_BYTES
        )
        if isinstance(reply, Unreadable):
            raise BrokerReplyError.malformed(reply)
        if isinstance(reply, BrokerRefused):
            raise PermissionError(reply.error)
        return ProcessOutcome(
            reply.result.returncode, reply.result.stdout.encode(), reply.result.stderr.encode()
        )


def _exchange(network: Network, address: str, request: bytes, timeout: float) -> bytes:
    """Send ``request`` and read the reply line, each wait bounded by ``timeout``."""
    connection = network.connect(address, timeout)
    try:
        connection.send(request, timeout)
        return _reply_line(connection, timeout)
    finally:
        connection.close()


def _reply_line(connection: Connection, timeout: float) -> bytes:
    """The broker's one reply line, read without buffering past the frame bound."""
    received = bytearray()
    while not received.endswith(b"\n"):
        chunk = connection.recv(min(_RECV_BYTES, MAX_FRAME_BYTES + 1 - len(received)), timeout)
        if not chunk:
            raise BrokerReplyError.no_reply()
        received += chunk
        if len(received) > MAX_FRAME_BYTES:
            raise BrokerReplyError.too_large()
    return bytes(received)


__all__ = ["BrokerReplyError", "BrokeredProcesses"]
