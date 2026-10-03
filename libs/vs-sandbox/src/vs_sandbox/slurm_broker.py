"""Host-owned, destination-constrained process broker for Slurm transports."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import socketserver
import subprocess
import threading
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ConfigDict, Field

from vs_slurm.api import SlurmConfig, SlurmSshTransport

if TYPE_CHECKING:
    from collections.abc import Sequence

_MAX_FRAME_BYTES = 16 * 1024 * 1024
_SSH_SUFFIX_LENGTH = 3
_RSYNC_OPERAND_COUNT = 2


class _ProcessCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    token: str
    argv: tuple[str, ...] = Field(min_length=1)
    stdin: str | None
    timeout: float = Field(gt=0)


class _ProcessReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    returncode: int
    stdout: str
    stderr: str


class SlurmProcessBrokerError(PermissionError):
    """A caller requested transport access outside the configured destination."""

    @classmethod
    def invalid_capability(cls) -> SlurmProcessBrokerError:
        """Reject an unknown broker capability."""
        return cls("invalid Slurm transport capability")

    @classmethod
    def unauthorized_destination(cls) -> SlurmProcessBrokerError:
        """Reject a destination other than the configured Slurm host."""
        return cls("Slurm transport request has an unauthorized destination")

    @classmethod
    def unauthorized_executable(cls) -> SlurmProcessBrokerError:
        """Reject a program outside the configured transport commands."""
        return cls("Slurm transport executable is not authorized")

    @classmethod
    def malformed_rsync(cls) -> SlurmProcessBrokerError:
        """Reject an rsync invocation outside the generated command shape."""
        return cls("Slurm rsync request is malformed")

    @classmethod
    def unauthorized_options(cls) -> SlurmProcessBrokerError:
        """Reject rsync options outside the staging contract."""
        return cls("Slurm rsync options are not authorized")

    @classmethod
    def remote_path_denied(cls) -> SlurmProcessBrokerError:
        """Reject remote paths outside the configured run namespace."""
        return cls("Slurm rsync path is outside the configured namespace")

    @classmethod
    def local_path_denied(cls) -> SlurmProcessBrokerError:
        """Reject host paths outside the declared run roots."""
        return cls("Slurm rsync path is outside the run-owned roots")


class _BrokerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, owner: SlurmProcessBroker) -> None:
        self.owner = owner
        super().__init__(str(owner.socket_path), _BrokerHandler)


class _BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        frame = self.rfile.readline(_MAX_FRAME_BYTES + 1)
        try:
            call = _parse_call(frame)
            server = cast("_BrokerServer", self.server)
            reply = server.owner.execute(call)
            payload = {"ok": True, "result": reply.model_dump(mode="json")}
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-930076 [BLE001]; this process boundary converts arbitrary transport and validation failures into one bounded protocol response, while narrower catches could terminate the broker thread without replying.
            payload = {"ok": False, "error": str(error)}
        self.wfile.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")


def _parse_call(frame: bytes) -> _ProcessCall:
    if not frame or len(frame) > _MAX_FRAME_BYTES or not frame.endswith(b"\n"):
        message = "invalid Slurm broker request"
        raise ValueError(message)
    return _ProcessCall.model_validate_json(frame)


class SlurmProcessBroker:
    """Execute one configured SSH transport without sharing host credentials."""

    def __init__(
        self,
        config: SlurmConfig,
        socket_path: Path,
        *,
        local_roots: Sequence[Path],
    ) -> None:
        """Bind the operator config, private socket, and allowed local roots."""
        transport = config.transport
        if not isinstance(transport, SlurmSshTransport):
            message = "Slurm process broker requires the SSH transport"
            raise TypeError(message)
        self._config = config
        self._transport = transport
        self.socket_path = socket_path
        self.token = secrets.token_urlsafe(32)
        self._local_roots = tuple(path.resolve() for path in local_roots)
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

    def execute(self, call: _ProcessCall) -> _ProcessReply:
        """Validate one request, then execute it with the host credential environment."""
        if not secrets.compare_digest(call.token, self.token):
            raise SlurmProcessBrokerError.invalid_capability()
        self._validate_argv(call.argv)
        completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-092714 [S603]; the broker validates argv against the operator-configured executables, exact destination, remote namespace, and local roots before invocation.
            call.argv,
            input=call.stdin,
            capture_output=True,
            text=True,
            timeout=min(call.timeout, float(self._config.transport_timeout_seconds)),
            check=False,
            env=os.environ.copy(),
            # As in vs_slurm's own transport: in its own process group, Ctrl-C
            # aimed at the run's terminal cannot kill an in-flight sbatch (its
            # job id would be lost) or the scancel a stopping gate requests.
            process_group=0,
        )
        return _ProcessReply(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def _validate_argv(self, argv: tuple[str, ...]) -> None:
        ssh = self._transport.ssh_command
        rsync = self._transport.rsync_command
        if argv[: len(ssh)] == ssh:
            suffix = argv[len(ssh) :]
            if len(suffix) != _SSH_SUFFIX_LENGTH or suffix[:2] != (
                "--",
                self._transport.host,
            ):
                raise SlurmProcessBrokerError.unauthorized_destination()
            return
        if argv[: len(rsync)] != rsync:
            raise SlurmProcessBrokerError.unauthorized_executable()
        self._validate_rsync(argv[len(rsync) :])

    def _validate_rsync(self, argv: tuple[str, ...]) -> None:
        try:
            separator = argv.index("--")
        except ValueError as error:
            raise SlurmProcessBrokerError.malformed_rsync() from error
        options = argv[:separator]
        operands = argv[separator + 1 :]
        expected_shell = shlex.join(self._transport.ssh_command)
        if len(operands) != _RSYNC_OPERAND_COUNT or not options or options[0] != "-a":
            raise SlurmProcessBrokerError.malformed_rsync()
        index = 1
        if index < len(options) and options[index] == "--delete":
            index += 1
        while index < len(options) and options[index].startswith("--exclude="):
            index += 1
        if options[index:] != ("-e", expected_shell):
            raise SlurmProcessBrokerError.unauthorized_options()
        remote_indexes = [
            position
            for position, operand in enumerate(operands)
            if operand.startswith(f"{self._transport.host}:")
        ]
        if len(remote_indexes) != 1:
            raise SlurmProcessBrokerError.unauthorized_destination()
        remote_index = remote_indexes[0]
        remote = operands[remote_index].split(":", 1)[1].rstrip("/")
        allowed_remote = PurePosixPath(self._config.remote_workspace_root) / self._config.name
        remote_path = PurePosixPath(remote)
        if remote_path != allowed_remote and allowed_remote not in remote_path.parents:
            raise SlurmProcessBrokerError.remote_path_denied()
        local = Path(operands[1 - remote_index].rstrip("/")).resolve()
        if not any(local == root or root in local.parents for root in self._local_roots):
            raise SlurmProcessBrokerError.local_path_denied()


__all__ = ["SlurmProcessBroker", "SlurmProcessBrokerError"]
