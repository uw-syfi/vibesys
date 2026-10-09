"""Host-owned broker that runs confined agent GPU commands through Slurm.

The agent's sandbox cannot reach the Slurm controller (its authentication
socket is not bound). It asks this broker instead, over a private Unix socket
guarded by a per-run token. The broker checks the working directory and the
operator limits, re-wraps the command in the agent's confinement policy, and
runs it with ``srun``, so moving GPU work into a job never loosens the
sandbox. Output streams back as it arrives; closing the connection cancels
the job.
"""

from __future__ import annotations

import base64
import contextlib
import json
import secrets
import socketserver
import threading
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ConfigDict, Field

from vs_sandbox.slurm_gpu import (
    GpuCommand,
    GpuJobRequest,
    GpuLauncher,
    SlurmGpuConfig,
    SlurmGpuLauncher,
)

if TYPE_CHECKING:
    import socket
    from collections.abc import Callable, Mapping, Sequence

SOCKET_ENV = "VIBESYS_GPU_BROKER_SOCKET"
TOKEN_ENV = "VIBESYS_GPU_BROKER_TOKEN"  # noqa: S105  # lint-waiver: LW-610003 [S105]; this is the name of the variable carrying the token, not a secret.
# > Renaming the constant to dodge the heuristic would hide what it names; the
# > value is an environment variable name with no credential in it.

_MAX_FRAME_BYTES = 4 * 1024 * 1024
# Variables that would point the job at the agent host's devices or at the
# broker itself. Slurm sets the device variables for the allocation.
_DROPPED_ENV_PREFIXES = ("VIBESYS_GPU_", "SLURM_")
_DROPPED_ENV = frozenset(
    {
        "CUDA_VISIBLE_DEVICES",
        "NVIDIA_VISIBLE_DEVICES",
        "GPU_DEVICE_ORDINAL",
        "HIP_VISIBLE_DEVICES",
        "ROCR_VISIBLE_DEVICES",
        "VIBESYS_AGENT_SANDBOX_GPUS",
    }
)
# Runs inside the confinement namespace, whose own working directory is the
# workspace root, and moves to the directory the agent ran the command from.
_CHDIR_SCRIPT = 'cd -- "$1" || exit 126; shift; exec "$@"'


class GpuCall(BaseModel):
    """One request from the confined client."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    token: str
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = Field(min_length=1)
    gpus: int | None = None
    time_minutes: int | None = None
    env: dict[str, str]


class SlurmGpuBrokerError(PermissionError):
    """A request outside the broker's capability."""

    @classmethod
    def invalid_capability(cls) -> SlurmGpuBrokerError:
        """Reject an unknown token."""
        return cls("invalid GPU broker capability")

    @classmethod
    def outside_workspace(cls) -> SlurmGpuBrokerError:
        """Reject a working directory outside the run's workspaces."""
        return cls("GPU commands must run from inside the run's workspace")


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, owner: SlurmGpuBroker) -> None:
        self.owner = owner
        super().__init__(str(owner.socket_path), _Handler)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        owner = cast("_Server", self.server).owner
        frame = self.rfile.readline(_MAX_FRAME_BYTES + 1)
        try:
            request, command = owner.prepare(_parse_call(frame))
        except (ValueError, PermissionError) as error:
            _send(self.connection, {"error": str(error)})
            return
        cancel = threading.Event()
        # A request sends nothing after its first line, so a readable socket
        # means the client went away: cancel the job, queued or running.
        threading.Thread(
            target=_cancel_on_hangup,
            args=(self.connection, cancel),
            name="vibesys-gpu-hangup",
            daemon=True,
        ).start()

        def write(chunk: bytes) -> None:
            if cancel.is_set():
                return
            try:
                _send(self.connection, {"output": base64.b64encode(chunk).decode()})
            except OSError:
                cancel.set()

        status = owner.launcher.run(request, command, write=write, cancel=cancel)
        if not cancel.is_set():
            try:
                _send(self.connection, {"exit": status})
            except OSError:
                return


def _parse_call(frame: bytes) -> GpuCall:
    if not frame.endswith(b"\n") or len(frame) > _MAX_FRAME_BYTES:
        message = "invalid GPU broker request"
        raise ValueError(message)
    return GpuCall.model_validate_json(frame)


def _send(connection: socket.socket, payload: Mapping[str, object]) -> None:
    connection.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")


def _cancel_on_hangup(connection: socket.socket, cancel: threading.Event) -> None:
    with contextlib.suppress(OSError):
        connection.recv(1)
    cancel.set()


class SlurmGpuBroker:
    """Serve confined GPU command requests for one run.

    *workspaces* are directories a command may run in, each confined to
    itself; *worktree_roots* hold one candidate workspace per child. *wrap*
    returns the argv that runs a command confined to a workspace.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-610005 [PLR0913]; the broker binds independent run facts supplied by its one composition site.
        # > Grouping these into a settings object would only move the same keyword
        # > arguments to that object's constructor at the single call site.
        self,
        config: SlurmGpuConfig,
        socket_path: Path,
        *,
        workspaces: Sequence[Path],
        worktree_roots: Sequence[Path],
        wrap: Callable[[Path, list[str]], list[str]],
        launcher: GpuLauncher | None = None,
    ) -> None:
        """Bind the operator limits, private socket, run workspaces, and confinement."""
        self._config = config
        self.socket_path = socket_path
        self.token = secrets.token_urlsafe(32)
        self._workspaces = tuple(path.resolve() for path in workspaces)
        self._worktree_roots = tuple(path.resolve() for path in worktree_roots)
        self._wrap = wrap
        self.launcher: GpuLauncher = launcher or SlurmGpuLauncher(config)
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._socket_identity: tuple[int, int] | None = None

    def start(self) -> None:
        """Bind the private socket and start serving requests."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        server = _Server(self)
        try:
            stat = self.socket_path.stat()
            self._socket_identity = (stat.st_dev, stat.st_ino)
            self.socket_path.chmod(0o600)
            thread = threading.Thread(target=server.serve_forever, name="vibesys-gpu-broker")
            thread.start()
            self._server = server
            self._thread = thread
        except BaseException:
            server.server_close()
            self._unlink_owned_socket()
            raise

    def close(self) -> None:
        """Stop accepting requests and remove the socket exactly once.

        In-flight requests are handled on daemon threads; a run that closes
        its broker is ending, and its jobs end with the connections the
        agent processes hold.
        """
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

    def prepare(self, call: GpuCall) -> tuple[GpuJobRequest, GpuCommand]:
        """Authorize *call* and return its job request and confined command."""
        if not secrets.compare_digest(call.token, self.token):
            raise SlurmGpuBrokerError.invalid_capability()
        request = self._config.request(call.gpus, call.time_minutes)
        cwd = Path(call.cwd).resolve()
        workspace = self._workspace_for(cwd)
        argv = self._wrap(
            workspace, ["/bin/sh", "-c", _CHDIR_SCRIPT, "vibesys-gpu", str(cwd), *call.argv]
        )
        env = {
            key: value
            for key, value in call.env.items()
            if key not in _DROPPED_ENV and not key.startswith(_DROPPED_ENV_PREFIXES)
        }
        return request, GpuCommand(argv=tuple(argv), cwd=workspace, env=env)

    def _workspace_for(self, cwd: Path) -> Path:
        for workspace in self._workspaces:
            if cwd == workspace or workspace in cwd.parents:
                return workspace
        for root in self._worktree_roots:
            if root in cwd.parents:
                return root / cwd.relative_to(root).parts[0]
        raise SlurmGpuBrokerError.outside_workspace()


__all__ = ["SOCKET_ENV", "TOKEN_ENV", "GpuCall", "SlurmGpuBroker", "SlurmGpuBrokerError"]
