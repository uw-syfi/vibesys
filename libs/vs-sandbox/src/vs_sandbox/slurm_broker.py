"""Host-owned, destination-constrained process broker for Slurm transports."""

from __future__ import annotations

import secrets
import socketserver
import threading
from typing import TYPE_CHECKING, Protocol, cast

from vs_decode.api import Unreadable, decode_json
from vs_sandbox.slurm_broker_protocol import (
    MAX_FRAME_BYTES,
    BrokerRan,
    BrokerRefused,
    BrokerReply,
    ProcessCall,
    ProcessReply,
)
from vs_sim.api import (
    ProcessGroupUnreaped,
    ProcessOutcome,
    ProcessOutputTooLarge,
    ProcessSpec,
    ProcessTimedOut,
)
from vs_slurm.api import SlurmTransportBrokerPolicy, SlurmTransportRequestError

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import Processes

# Kept as the public broker spelling; authorization is owned by vs-slurm's transport policy.
SlurmProcessBrokerError = SlurmTransportRequestError


class _ReplyWriter(Protocol):
    def write(self, data: bytes, /) -> object:
        """Write one complete broker response."""
        ...


class _BrokerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, owner: SlurmProcessBroker) -> None:
        self.owner = owner
        super().__init__(str(owner.socket_path), _BrokerHandler)


class _BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        frame = self.rfile.readline(MAX_FRAME_BYTES + 1)
        try:
            call = _parse_call(frame)
            server = cast("_BrokerServer", self.server)
            payload: BrokerReply = BrokerRan(ok=True, result=server.owner.execute(call))
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-930076 [BLE001]; this process boundary converts arbitrary transport and validation failures into one bounded protocol response, while narrower catches could terminate the broker thread without replying.
            payload = BrokerRefused(ok=False, error=str(error))
        _write_reply(self.wfile, payload)


def _write_reply(stream: _ReplyWriter, payload: BrokerReply) -> None:
    """Finish a request unless its client has already abandoned the connection."""
    try:
        stream.write(payload.model_dump_json().encode() + b"\n")
    except (BrokenPipeError, ConnectionResetError):
        return


class BrokerRequestError(ValueError):
    """A request frame that is not a valid call; names the problems, never the input."""

    def __init__(self, unreadable: Unreadable) -> None:
        """Describe the unreadable frame."""
        super().__init__(str(unreadable))


def _parse_call(frame: bytes) -> ProcessCall:
    if not frame or len(frame) > MAX_FRAME_BYTES or not frame.endswith(b"\n"):
        message = "invalid Slurm broker request"
        raise ValueError(message)
    call = decode_json(frame, ProcessCall, source="Slurm broker request", max_bytes=MAX_FRAME_BYTES)
    if isinstance(call, Unreadable):
        raise BrokerRequestError(call)
    return call


class SlurmProcessBroker:
    """Execute one policy-authorized Slurm transport without sharing host credentials."""

    def __init__(
        self,
        policy: SlurmTransportBrokerPolicy,
        socket_path: Path,
        *,
        processes: Processes,
    ) -> None:
        """Bind the transport policy and private socket.

        ``processes`` runs each validated transport program with the host's credentials.
        """
        self._policy = policy
        self.socket_path = socket_path
        self.token = secrets.token_urlsafe(32)
        self._processes = processes
        self._server: _BrokerServer | None = None
        self._thread: threading.Thread | None = None
        self._socket_identity: tuple[int, int] | None = None

    def start(self) -> None:
        """Bind the private run socket and start serving transport requests."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        server = _BrokerServer(self)
        try:
            stat = self.socket_path.stat()
            self._socket_identity = (stat.st_dev, stat.st_ino)
            self.socket_path.chmod(0o600)
            thread = threading.Thread(target=server.serve_forever, name="vibesys-slurm-broker")
            thread.start()
            self._server = server
            self._thread = thread
        except BaseException:
            server.server_close()
            self._unlink_owned_socket()
            raise

    def close(self) -> None:
        """Stop serving and remove the socket exactly once."""
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join()
        self._unlink_owned_socket()

    def _unlink_owned_socket(self) -> None:
        identity, self._socket_identity = self._socket_identity, None
        if identity is None:
            return
        try:
            stat = self.socket_path.stat()
        except FileNotFoundError:
            return
        if (stat.st_dev, stat.st_ino) == identity:
            self.socket_path.unlink(missing_ok=True)

    def execute(self, call: ProcessCall) -> ProcessReply:
        """Validate one request, then execute it with the host credential environment."""
        if not secrets.compare_digest(call.token, self.token):
            raise SlurmTransportRequestError.invalid_capability()
        spec = ProcessSpec(call.argv, input=b"" if call.stdin is None else call.stdin.encode())
        timeout = min(call.timeout, self._policy.timeout_seconds)
        # As in vs_slurm's own transport, the program runs in its own process group:
        # Ctrl-C aimed at the run's terminal cannot kill an in-flight sbatch (its job
        # id would be lost) or the scancel a stopping gate requests.
        run = self._policy.run(
            ProcessSpec(spec.argv, input=spec.input, max_output_bytes=call.max_output_bytes),
            processes=self._processes,
            timeout=timeout,
        )
        match run:
            case ProcessOutcome(returncode=returncode, stdout=stdout, stderr=stderr):
                return ProcessReply(
                    returncode=returncode, stdout=stdout.decode(), stderr=stderr.decode()
                )
            case ProcessTimedOut():
                message = f"Slurm transport timed out after {timeout:g} seconds"
                raise TimeoutError(message)
            case ProcessGroupUnreaped(detail=detail):
                message = f"Slurm transport timed out after {timeout:g} seconds; {detail}"
                raise TimeoutError(message)
            case ProcessOutputTooLarge(limit=limit, stream=stream):
                message = f"Slurm transport wrote more than {limit} bytes to {stream}"
                raise OSError(message)


__all__ = ["SlurmProcessBroker", "SlurmProcessBrokerError"]
