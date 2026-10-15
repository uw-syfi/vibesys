"""Host-owned broker that runs an agent's Slurm requests on the submit host.

The agent runs in a Docker container that cannot reach the Slurm controller
(its credentials and tools stay on the host). It asks this broker instead,
over a private Unix socket that is bind-mounted into the container and guarded
by a per-run token. One operation exists, ``gpu``: run the agent's command in
a new Slurm job on the broker's :class:`~vs_sandbox.job_confinement.JobConfinement`.
The job runs on a compute node, where the agent's container does not exist, so
the broker confines it by an explicit policy rather than by whatever the agent
runs in. Trusted evaluation is not an operation: the framework runs it through
the evaluation executor, and the agent requests it through the evaluation tool.

The agent's workspace is mounted in its container at the same path as on the
host, so the working directory a request names is a path the host can check
and use. The broker accepts a directory only inside the run's roots. Output
streams back as it arrives; closing the connection cancels the job.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from vs_decode.api import Unreadable, decode_json
from vs_sandbox.agent_gpu import (
    AgentGpuConfig,
    GpuCommand,
    GpuLauncher,
)
from vs_sandbox.host_command_client import SOCKET_ENV, TOKEN_ENV

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from vs_sandbox.job_confinement import JobConfinement
    from vs_sim.api import Connection, Event, Listener, Network, Threads, Worker

_MAX_FRAME_BYTES = 4 * 1024 * 1024
# How long close() waits for cancelled jobs to be torn down (their scancel round trips).
_DRAIN_SECONDS = 60.0
_SEND_BOUND_SECONDS = 30.0
"""How long one frame may wait for the client to make room before the broker gives up on it.

A client that stops reading would otherwise block the job's output writer, so the job, and
the Slurm allocation it holds, would never end; giving up cancels the job.
"""
_LOG = logging.getLogger(__name__)
# Variables that would point the job at the agent host's devices or at the
# broker itself. Slurm sets the device variables for the allocation.
_DROPPED_ENV_PREFIXES = ("VIBESYS_COMMAND_BROKER_", "SLURM_")
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
# Variables that describe the agent container's own filesystem and identity. The
# job runs on a host node with its own, so these are never carried over.
_CONTAINER_IDENTITY_ENV = frozenset(
    {
        "HOME",
        "HOSTNAME",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "LOGNAME",
        "OLDPWD",
        "PATH",
        "PWD",
        "PYTHONHOME",
        "PYTHONPATH",
        "SHELL",
        "TMPDIR",
        "USER",
        "VIRTUAL_ENV",
    }
)
_CONTAINER_IDENTITY_ENV_PREFIXES = ("XDG_",)
# What a job inherits from the submit host: enough to find its programs and
# libraries, nothing that carries a credential.
_HOST_BASELINE_ENV = frozenset(
    {
        "CUDA_HOME",
        "CUDA_PATH",
        "HIP_PATH",
        "HOME",
        "LANG",
        "LANGUAGE",
        "LD_LIBRARY_PATH",
        "LOGNAME",
        "PATH",
        "ROCM_PATH",
        "SHELL",
        "TMPDIR",
        "TZ",
        "USER",
    }
)
_HOST_BASELINE_ENV_PREFIXES = ("LC_",)
# Runs inside the confinement namespace, whose own working directory is the
# workspace root, and moves to the directory the agent ran the command from.
_CHDIR_SCRIPT = 'cd -- "$1" || exit 126; shift; exec "$@"'


class GpuCall(BaseModel):
    """A request to run a command in a Slurm job."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    op: Literal["gpu"]
    token: str
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = Field(min_length=1)
    gpus: int | None = None
    time_minutes: int | None = None
    env: dict[str, str]


class HostCommandBrokerError(PermissionError):
    """A request outside the broker's capability."""

    @classmethod
    def invalid_capability(cls) -> HostCommandBrokerError:
        """Reject an unknown token."""
        return cls("invalid command broker capability")

    @classmethod
    def outside_workspace(cls) -> HostCommandBrokerError:
        """Reject a working directory outside the run's workspaces."""
        return cls("commands must run from inside the run's workspace")

    @classmethod
    def unsupported(cls, operation: str) -> HostCommandBrokerError:
        """Reject an operation this run does not offer."""
        return cls(f"this run does not offer the {operation} operation")


@dataclass(frozen=True, slots=True)
class RunRoots:
    """The directories a request may run in.

    *workspaces* are directories a command may run in, each confined to
    itself. A broker serves one session, so it lists that session's own
    workspace and never a sibling candidate's.
    """

    workspaces: Sequence[Path]


@dataclass(frozen=True, slots=True)
class GpuCommands:
    """What the broker needs to offer the ``gpu`` operation.

    *host_env* is the submit host's environment a job may inherit; only its
    baseline subset reaches the job. *confinement* is the job's explicit
    confinement policy. *launcher* starts the job's ``srun``.
    """

    config: AgentGpuConfig
    confinement: JobConfinement
    host_env: Mapping[str, str]
    launcher: GpuLauncher


@dataclass(frozen=True, slots=True)
class BrokerTransport:
    """The I/O a broker runs on: where its socket lives and the threads that serve it."""

    network: Network
    threads: Threads


@dataclass(frozen=True, slots=True)
class _Job:
    """One authorized request, ready to run."""

    run: Callable[[Callable[[bytes], None], Event], int]


def _read_frame(connection: Connection) -> bytes:
    """Read up to the first newline, or until the frame is too long or the peer closes."""
    frame = b""
    while b"\n" not in frame and len(frame) <= _MAX_FRAME_BYTES:
        chunk = connection.recv(_MAX_FRAME_BYTES + 1 - len(frame))
        if not chunk:
            break
        frame += chunk
    return frame[: frame.find(b"\n") + 1] if b"\n" in frame else frame


class BrokerRequestError(ValueError):
    """A request frame that is not a valid call; names the problems, never the input."""

    def __init__(self, unreadable: Unreadable) -> None:
        """Describe the unreadable frame."""
        super().__init__(str(unreadable))


def _parse_call(frame: bytes) -> GpuCall:
    if not frame.endswith(b"\n") or len(frame) > _MAX_FRAME_BYTES:
        message = "invalid command broker request"
        raise ValueError(message)
    call = decode_json(frame, GpuCall, source="command broker request", max_bytes=_MAX_FRAME_BYTES)
    if isinstance(call, Unreadable):
        raise BrokerRequestError(call)
    return call


def _send(connection: Connection, payload: Mapping[str, object]) -> None:
    """Send one frame; ``TimeoutError`` when the client read nothing for the send bound."""
    connection.send(
        json.dumps(payload, separators=(",", ":")).encode() + b"\n", _SEND_BOUND_SECONDS
    )


def _cancel_on_hangup(connection: Connection, cancel: Event) -> None:
    with contextlib.suppress(OSError):
        connection.recv(1)
    cancel.set()


def job_environment(host_env: Mapping[str, str], requested: Mapping[str, str]) -> dict[str, str]:
    """Return the environment a brokered job runs with.

    The submit host's baseline (programs, libraries, locale) with the agent's
    own variables laid over it. The agent runs in a container, so the
    variables that describe the container's filesystem and identity (``PATH``,
    ``HOME``, and the like) are not carried over, and neither are device
    selections or the broker's own variables: Slurm sets the devices for the
    allocation, and the job never needs the broker.
    """
    baseline = {
        key: value
        for key, value in host_env.items()
        if key in _HOST_BASELINE_ENV or key.startswith(_HOST_BASELINE_ENV_PREFIXES)
    }
    carried = {
        key: value
        for key, value in requested.items()
        if key not in _DROPPED_ENV
        and key not in _CONTAINER_IDENTITY_ENV
        and not key.startswith((*_DROPPED_ENV_PREFIXES, *_CONTAINER_IDENTITY_ENV_PREFIXES))
    }
    return {**baseline, **carried}


class HostCommandBroker:
    """Serve one run's Slurm requests over a private Unix socket.

    *roots* bound where a command may run. *gpu* is the operation the run offers;
    a broker without it refuses every request by name. *transport* is
    where the socket is bound and what runs the accept loop and the handlers.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        roots: RunRoots,
        transport: BrokerTransport,
        gpu: GpuCommands | None = None,
    ) -> None:
        """Bind the private socket, the run's roots, and the operations it offers."""
        self.socket_path = socket_path
        self.token = secrets.token_urlsafe(32)
        self._network = transport.network
        self._threads = transport.threads
        self._workspaces = tuple(path.resolve() for path in roots.workspaces)
        self._gpu = gpu
        self._launcher: GpuLauncher | None = None if gpu is None else gpu.launcher
        self._listener: Listener | None = None
        self._acceptor: Worker | None = None
        self._in_flight: set[Event] = set()
        self._idle = self._threads.condition(self._threads.lock())
        self._closing = False

    def start(self) -> None:
        """Bind the private socket and start serving requests."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        listener = self._network.listen(str(self.socket_path))
        try:
            # The socket is a file only on a filesystem network; any other has nothing to restrict.
            with contextlib.suppress(FileNotFoundError):
                self.socket_path.chmod(0o600)
            self._acceptor = self._threads.spawn(
                lambda: self._accept_loop(listener), name="vibesys-command-broker", daemon=False
            )
        except BaseException:
            listener.close()
            raise
        self._listener = listener

    def close(self, drain_seconds: float = _DRAIN_SECONDS) -> None:
        """Stop accepting requests and remove the socket exactly once.

        Jobs still running are cancelled, and ``close`` waits (up to
        *drain_seconds*) for their handlers to finish, so the Slurm jobs they
        asked for are cancelled before the run is gone rather than left to the
        cluster's time limit.
        """
        listener, self._listener = self._listener, None
        acceptor, self._acceptor = self._acceptor, None
        if listener is None:
            return
        with self._idle:
            self._closing = True
        listener.close()
        if acceptor is not None:
            acceptor.join()
        with self._idle:
            for cancel in self._in_flight:
                cancel.set()
            self._idle.wait_for(lambda: not self._in_flight, drain_seconds)

    def _accept_loop(self, listener: Listener) -> None:
        while True:
            try:
                connection = listener.accept()
            except OSError:
                return  # the listener was closed
            self._threads.spawn(lambda c=connection: self._handle(c), name="vibesys-command-job")

    def _track(self, cancel: Event) -> bool:
        """Register a request as in flight; ``False`` once the broker is closing."""
        with self._idle:
            if self._closing:
                return False
            self._in_flight.add(cancel)
            return True

    def _untrack(self, cancel: Event) -> None:
        with self._idle:
            self._in_flight.discard(cancel)
            self._idle.notify_all()

    def _handle(self, connection: Connection) -> None:
        try:
            self._serve(connection)
        finally:
            connection.close()

    def _serve(self, connection: Connection) -> None:
        try:
            frame = _read_frame(connection)
        except OSError:
            return  # the client went away before finishing its request
        try:
            job = self.prepare(_parse_call(frame))
        except (ValueError, PermissionError) as error:
            with contextlib.suppress(OSError):
                _send(connection, {"error": str(error)})
            return
        cancel = self._threads.event()
        try:
            if self._track(cancel):
                self._run(connection, job, cancel)
        finally:
            self._untrack(cancel)

    def _run(self, connection: Connection, job: _Job, cancel: Event) -> None:
        # A request sends nothing after its first line, so a readable socket
        # means the client went away: cancel the job, queued or running.
        self._threads.spawn(
            lambda: _cancel_on_hangup(connection, cancel), name="vibesys-command-hangup"
        )

        def write(chunk: bytes) -> None:
            if cancel.is_set():
                return
            try:
                _send(connection, {"output": base64.b64encode(chunk).decode()})
            except TimeoutError:
                _LOG.warning(
                    "a command broker client read nothing for %g s; cancelling its job",
                    _SEND_BOUND_SECONDS,
                )
                cancel.set()
            except OSError:
                cancel.set()

        status = job.run(write, cancel)
        if cancel.is_set():
            return
        try:
            _send(connection, {"exit": status})
        except OSError:
            return

    def prepare(self, call: GpuCall) -> _Job:
        """Authorize *call* and return the job it asks for."""
        if not secrets.compare_digest(call.token, self.token):
            raise HostCommandBrokerError.invalid_capability()
        cwd = Path(call.cwd).resolve()
        workspace = self._workspace_for(cwd)
        return self._prepare_gpu(call, cwd, workspace)

    def _prepare_gpu(self, call: GpuCall, cwd: Path, workspace: Path) -> _Job:
        gpu = self._gpu
        launcher = self._launcher
        if gpu is None or launcher is None:
            raise HostCommandBrokerError.unsupported("gpu")
        request = gpu.config.request(call.gpus, call.time_minutes)
        argv = gpu.confinement.wrap(
            workspace, ["/bin/sh", "-c", _CHDIR_SCRIPT, "vibesys-gpu", str(cwd), *call.argv]
        )
        command = GpuCommand(
            argv=tuple(argv), cwd=workspace, env=job_environment(gpu.host_env, call.env)
        )
        return _Job(
            run=lambda write, cancel: launcher.run(request, command, write=write, cancel=cancel)
        )

    def _workspace_for(self, cwd: Path) -> Path:
        for workspace in self._workspaces:
            if cwd == workspace or workspace in cwd.parents:
                return workspace
        raise HostCommandBrokerError.outside_workspace()


__all__ = [
    "SOCKET_ENV",
    "TOKEN_ENV",
    "BrokerTransport",
    "GpuCall",
    "GpuCommands",
    "HostCommandBroker",
    "HostCommandBrokerError",
    "RunRoots",
    "job_environment",
]
