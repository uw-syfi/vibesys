"""Confined client for the host-owned Slurm process broker.

``run_brokered_process`` stands in for ``subprocess.run`` behind ``vs_slurm``'s process
seam, so it keeps that call's contract: a broker nobody serves is ``FileNotFoundError``, a
call the broker does not answer in time is ``subprocess.TimeoutExpired``, and a refusal is
``PermissionError``. A reply outside the broker protocol is ``BrokerReplyError``.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vs_sandbox.slurm_broker_protocol import (
    BROKER_REPLY,
    MAX_FRAME_BYTES,
    BrokerRefused,
    ProcessCall,
)
from vs_sim.api import UnixNetwork

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vs_sim.api import Connection, Network

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
    def malformed(cls, error: ValidationError) -> BrokerReplyError:
        """The reply line is not a broker reply."""
        return cls(f"Slurm broker reply is malformed: {error.error_count()} errors")


def run_brokered_process(  # noqa: PLR0913  # lint-waiver: LW-241403 [PLR0913]; the first five are ``vs_slurm``'s process seam, which this function implements, and ``network`` is its one transport seam; bundling any of them into an object would break the drop-in ``subprocess.run`` shape callers rely on.
    socket_path: Path,
    token: str,
    argv: Sequence[str],
    *,
    stdin: str | None,
    timeout: float,
    network: Network | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute one process through the host broker's constrained capability.

    ``timeout`` bounds each wait on the broker: to connect, to send, and for each part
    of the reply.

    Raises:
        FileNotFoundError: nothing listens on ``socket_path``.
        subprocess.TimeoutExpired: the broker did not answer within ``timeout``.
        PermissionError: the broker refused the call or could not run it.
        BrokerReplyError: the broker hung up early or replied outside its protocol.
    """
    request = ProcessCall.model_construct(
        token=token, argv=tuple(argv), stdin=stdin, timeout=timeout
    )
    try:
        line = _exchange(
            network or UnixNetwork(),
            str(socket_path),
            request.model_dump_json().encode() + b"\n",
            timeout,
        )
    except ConnectionRefusedError as error:
        raise FileNotFoundError(str(error)) from error
    except TimeoutError as error:
        raise subprocess.TimeoutExpired(list(argv), timeout) from error
    try:
        reply = BROKER_REPLY.validate_json(line)
    except ValidationError as error:
        raise BrokerReplyError.malformed(error) from error
    if isinstance(reply, BrokerRefused):
        raise PermissionError(reply.error)
    return subprocess.CompletedProcess(
        args=tuple(argv),
        returncode=reply.result.returncode,
        stdout=reply.result.stdout,
        stderr=reply.result.stderr,
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


__all__ = ["BrokerReplyError", "run_brokered_process"]
