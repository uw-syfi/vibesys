"""Host-owned broker that runs an agent's Slurm requests on the submit host.

The agent runs in a Docker container that cannot reach the Slurm controller
(its credentials and tools stay on the host). It asks this broker instead,
over a private Unix socket that is bind-mounted into the container and guarded
by a per-run token. Two operations exist, and a run composes the ones it
offers:

* ``gpu``: run the agent's command in a new Slurm job on the broker's
  :class:`~vs_sandbox.job_confinement.JobConfinement`. The job runs on a
  compute node, where the agent's container does not exist, so the broker
  confines it by an explicit policy rather than by whatever the agent runs in.
* ``gate``: run the framework's planned accuracy or benchmark gate through a
  :class:`GateRunner`. The agent names only which gate and, for a benchmark,
  where the result goes; it never supplies a command.

The agent's workspace is mounted in its container at the same path as on the
host, so the working directory a request names is a path the host can check
and use. The broker accepts a directory only inside the run's roots. Output
streams back as it arrives; closing the connection cancels the job.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import socketserver
import stat
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from vs_sandbox.benchmark_output import (
    OUTPUT_ARGUMENT_COUNT,
    BenchmarkOutputKind,
    classify_benchmark_output,
)
from vs_sandbox.host_command_client import SOCKET_ENV, TOKEN_ENV
from vs_sandbox.slurm_gpu import (
    GpuCommand,
    GpuLauncher,
    SlurmGpuConfig,
    SlurmGpuLauncher,
)

if TYPE_CHECKING:
    import socket
    from collections.abc import Callable, Mapping, Sequence

    from vs_sandbox.job_confinement import JobConfinement

_MAX_FRAME_BYTES = 4 * 1024 * 1024
# How often the serving loop checks for shutdown; bounds close() latency.
_POLL_SECONDS = 0.05
_MAX_RELAYED_FILE_BYTES = 8 * 1024 * 1024
# The directory the framework's own benchmark result path names; see ``benchmark_output``.
_FRAMEWORK_TMP = "/tmp"  # noqa: S108  # lint-waiver: LW-954393 [S108]; the fixed framework result directory, not a scratch choice.
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


class GateKind(StrEnum):
    """The trusted gates an agent may run through the broker."""

    ACCURACY = "accuracy"
    BENCHMARK = "benchmark"


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


class GateCall(BaseModel):
    """A request to run a planned trusted gate."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    op: Literal["gate"]
    token: str
    kind: GateKind
    arguments: tuple[str, ...] = ()
    cwd: str = Field(min_length=1)


type Call = Annotated[GpuCall | GateCall, Field(discriminator="op")]
_CALL = TypeAdapter[GpuCall | GateCall](Call)


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

    @classmethod
    def invalid_gate_arguments(cls, kind: GateKind) -> HostCommandBrokerError:
        """Reject gate arguments that differ from the plan."""
        return cls(f"invalid arguments for the {kind.value} gate")


class GateRunner(Protocol):
    """Runs one planned trusted gate to completion.

    :class:`~vs_sandbox.gate_runners.SlurmCommandGateRunner` and
    :class:`~vs_sandbox.gate_runners.SrunGateRunner` are the real ones. The
    broker has already validated *arguments* against the plan.
    """

    def run(
        self,
        kind: GateKind,
        arguments: Sequence[str],
        *,
        cwd: Path,
        write: Callable[[bytes], None],
        cancel: threading.Event,
    ) -> int:
        """Run the gate from *cwd*, stream its output to *write*, stop when *cancel* is set."""
        ...


@dataclass(frozen=True, slots=True)
class RunRoots:
    """The directories a request may run in.

    *workspaces* are directories a command may run in, each confined to
    itself; *worktree_roots* hold one candidate workspace per child.
    """

    workspaces: Sequence[Path]
    worktree_roots: Sequence[Path] = ()


@dataclass(frozen=True, slots=True)
class GpuCommands:
    """What the broker needs to offer the ``gpu`` operation.

    *host_env* is the submit host's environment a job may inherit; only its
    baseline subset reaches the job. *confinement* is the job's explicit
    confinement policy.
    """

    config: SlurmGpuConfig
    confinement: JobConfinement
    host_env: Mapping[str, str]
    launcher: GpuLauncher | None = None


@dataclass(frozen=True, slots=True)
class Gates:
    """What the broker needs to offer the ``gate`` operation.

    *benchmark_output_argument* is the benchmark's declared result argument,
    or ``None`` when the benchmark takes none.
    """

    runner: GateRunner
    benchmark_output_argument: str | None = None


@dataclass(frozen=True, slots=True)
class _Job:
    """One authorized request, ready to run."""

    run: Callable[[Callable[[bytes], None], threading.Event], int]
    #: ``(host file, path in the caller)``: the result to relay once the job ends.
    relay: tuple[Path, str] | None = None


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, owner: HostCommandBroker) -> None:
        self.owner = owner
        super().__init__(str(owner.socket_path), _Handler)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        owner = cast("_Server", self.server).owner
        frame = self.rfile.readline(_MAX_FRAME_BYTES + 1)
        try:
            job = owner.prepare(_parse_call(frame))
        except (ValueError, PermissionError) as error:
            _send(self.connection, {"error": str(error)})
            return
        cancel = threading.Event()
        # A request sends nothing after its first line, so a readable socket
        # means the client went away: cancel the job, queued or running.
        threading.Thread(
            target=_cancel_on_hangup,
            args=(self.connection, cancel),
            name="vibesys-command-hangup",
            daemon=True,
        ).start()

        def write(chunk: bytes) -> None:
            if cancel.is_set():
                return
            try:
                _send(self.connection, {"output": base64.b64encode(chunk).decode()})
            except OSError:
                cancel.set()

        try:
            status = job.run(write, cancel)
            if cancel.is_set():
                return
            try:
                if job.relay is not None:
                    _relay_file(self.connection, *job.relay)
                _send(self.connection, {"exit": status})
            except OSError:
                return
        finally:
            if job.relay is not None:
                job.relay[0].unlink(missing_ok=True)


def _parse_call(frame: bytes) -> GpuCall | GateCall:
    if not frame.endswith(b"\n") or len(frame) > _MAX_FRAME_BYTES:
        message = "invalid command broker request"
        raise ValueError(message)
    return _CALL.validate_json(frame)


def _send(connection: socket.socket, payload: Mapping[str, object]) -> None:
    connection.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")


def _relay_file(connection: socket.socket, host_file: Path, caller_path: str) -> None:
    """Send the result a gate wrote at *host_file*, to be written at *caller_path*."""
    content = _read_owned_file(host_file)
    if content is None:
        return
    _send(
        connection,
        {"file": {"path": caller_path, "data": base64.b64encode(content).decode()}},
    )


def _read_owned_file(path: Path) -> bytes | None:
    """Read a regular file this process owns, or ``None`` if there is no such file."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_size > _MAX_RELAYED_FILE_BYTES
        ):
            return None
        return handle.read()


def _reserve_result_file() -> Path:
    """Create an empty, private result file under the host's ``/tmp``.

    The name is unguessable and the file is created exclusively and without
    following links, so nothing else can claim it before the gate writes it.
    """
    path = Path(f"{_FRAMEWORK_TMP}/vibesys-framework-benchmark-{secrets.token_hex(16)}.json")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    return path


def _cancel_on_hangup(connection: socket.socket, cancel: threading.Event) -> None:
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

    *roots* bound where a command may run. *gpu* and *gates* are the operations
    the run offers; a request for another is refused by name.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        roots: RunRoots,
        gpu: GpuCommands | None = None,
        gates: Gates | None = None,
    ) -> None:
        """Bind the private socket, the run's roots, and the operations it offers."""
        self.socket_path = socket_path
        self.token = secrets.token_urlsafe(32)
        self._workspaces = tuple(path.resolve() for path in roots.workspaces)
        self._worktree_roots = tuple(path.resolve() for path in roots.worktree_roots)
        self._gpu = gpu
        self._gates = gates
        self._launcher: GpuLauncher | None = (
            None if gpu is None else (gpu.launcher or SlurmGpuLauncher(gpu.config))
        )
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._socket_identity: tuple[int, int] | None = None

    def start(self) -> None:
        """Bind the private socket and start serving requests."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        server = _Server(self)
        try:
            info = self.socket_path.stat()
            self._socket_identity = (info.st_dev, info.st_ino)
            self.socket_path.chmod(0o600)
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": _POLL_SECONDS},
                name="vibesys-command-broker",
            )
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
            info = self.socket_path.stat()
        except FileNotFoundError:
            return
        if (info.st_dev, info.st_ino) == identity:
            self.socket_path.unlink(missing_ok=True)

    def prepare(self, call: GpuCall | GateCall) -> _Job:
        """Authorize *call* and return the job it asks for."""
        if not secrets.compare_digest(call.token, self.token):
            raise HostCommandBrokerError.invalid_capability()
        cwd = Path(call.cwd).resolve()
        workspace = self._workspace_for(cwd)
        if isinstance(call, GpuCall):
            return self._prepare_gpu(call, cwd, workspace)
        return self._prepare_gate(call, cwd)

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

    def _prepare_gate(self, call: GateCall, cwd: Path) -> _Job:
        gates = self._gates
        if gates is None:
            raise HostCommandBrokerError.unsupported("gate")
        arguments, relay = self._gate_arguments(call, gates)
        return _Job(
            run=lambda write, cancel: gates.runner.run(
                call.kind, arguments, cwd=cwd, write=write, cancel=cancel
            ),
            relay=relay,
        )

    @staticmethod
    def _gate_arguments(
        call: GateCall, gates: Gates
    ) -> tuple[tuple[str, ...], tuple[Path, str] | None]:
        """Validate a gate's arguments against the plan; return them and any file to relay.

        An accuracy gate takes none. A benchmark takes none, or its declared
        result argument and one allowed result path. A result under the
        caller's ``/tmp`` is redirected to a private file of the broker's
        and relayed back when the gate ends, because the caller's ``/tmp`` is
        not the gate host's.
        """
        arguments = call.arguments
        output_argument = gates.benchmark_output_argument
        if call.kind is GateKind.ACCURACY or output_argument is None:
            if arguments:
                raise HostCommandBrokerError.invalid_gate_arguments(call.kind)
            return (), None
        if len(arguments) != OUTPUT_ARGUMENT_COUNT or arguments[0] != output_argument:
            raise HostCommandBrokerError.invalid_gate_arguments(call.kind)
        kind = classify_benchmark_output(arguments[1])
        if kind is None:
            raise HostCommandBrokerError.invalid_gate_arguments(call.kind)
        if kind is BenchmarkOutputKind.WORKSPACE:
            return arguments, None
        host_file = _reserve_result_file()
        return (output_argument, str(host_file)), (host_file, arguments[1])

    def _workspace_for(self, cwd: Path) -> Path:
        for workspace in self._workspaces:
            if cwd == workspace or workspace in cwd.parents:
                return workspace
        for root in self._worktree_roots:
            if root in cwd.parents:
                return root / cwd.relative_to(root).parts[0]
        raise HostCommandBrokerError.outside_workspace()


__all__ = [
    "SOCKET_ENV",
    "TOKEN_ENV",
    "GateCall",
    "GateKind",
    "GateRunner",
    "Gates",
    "GpuCall",
    "GpuCommands",
    "HostCommandBroker",
    "HostCommandBrokerError",
    "RunRoots",
    "job_environment",
]
