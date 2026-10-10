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
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from vs_sandbox.agent_gpu import (
    AgentGpuConfig,
    AgentGpuLauncher,
    GpuCommand,
    GpuLauncher,
)
from vs_sandbox.benchmark_output import (
    OUTPUT_ARGUMENT_COUNT,
    BenchmarkOutputKind,
    classify_benchmark_output,
)
from vs_sandbox.host_command_client import SOCKET_ENV, TOKEN_ENV
from vs_sim.api import Network, OsThreads, Threads, UnixNetwork

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from vs_sandbox.job_confinement import JobConfinement
    from vs_sim.api import Connection, Event, Listener, Worker

_MAX_FRAME_BYTES = 4 * 1024 * 1024
_MAX_RELAYED_FILE_BYTES = 8 * 1024 * 1024
# How long close() waits for cancelled jobs to be torn down (their scancel round trips).
_DRAIN_SECONDS = 60.0
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

    :class:`~vs_sandbox.gate_runners.SlurmCommandGateRunner` is the real one. The
    broker has already validated *arguments* against the plan.
    """

    def run(
        self,
        kind: GateKind,
        arguments: Sequence[str],
        *,
        cwd: Path,
        write: Callable[[bytes], None],
        cancel: Event,
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

    config: AgentGpuConfig
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
class BrokerTransport:
    """The I/O a broker runs on: where its socket lives and the threads that serve it."""

    network: Network = field(default_factory=UnixNetwork)
    threads: Threads = field(default_factory=OsThreads)


@dataclass(frozen=True, slots=True)
class _Job:
    """One authorized request, ready to run."""

    run: Callable[[Callable[[bytes], None], Event], int]
    #: ``(host file, path in the caller)``: the result to relay once the job ends.
    relay: tuple[Path, str] | None = None


def _read_frame(connection: Connection) -> bytes:
    """Read up to the first newline, or until the frame is too long or the peer closes."""
    frame = b""
    while b"\n" not in frame and len(frame) <= _MAX_FRAME_BYTES:
        chunk = connection.recv(_MAX_FRAME_BYTES + 1 - len(frame))
        if not chunk:
            break
        frame += chunk
    return frame[: frame.find(b"\n") + 1] if b"\n" in frame else frame


def _parse_call(frame: bytes) -> GpuCall | GateCall:
    if not frame.endswith(b"\n") or len(frame) > _MAX_FRAME_BYTES:
        message = "invalid command broker request"
        raise ValueError(message)
    return _CALL.validate_json(frame)


def _send(connection: Connection, payload: Mapping[str, object]) -> None:
    connection.send(json.dumps(payload, separators=(",", ":")).encode() + b"\n")


def _relay_file(connection: Connection, host_file: Path, caller_path: str) -> None:
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

    *roots* bound where a command may run. *gpu* and *gates* are the operations
    the run offers; a request for another is refused by name. *transport* is
    where the socket is bound and what runs the accept loop and the handlers;
    production uses the defaults, tests pass simulators.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        roots: RunRoots,
        gpu: GpuCommands | None = None,
        gates: Gates | None = None,
        transport: BrokerTransport | None = None,
    ) -> None:
        """Bind the private socket, the run's roots, and the operations it offers."""
        self.socket_path = socket_path
        self.token = secrets.token_urlsafe(32)
        transport = transport or BrokerTransport()
        self._network = transport.network
        self._threads = transport.threads
        self._workspaces = tuple(path.resolve() for path in roots.workspaces)
        self._worktree_roots = tuple(path.resolve() for path in roots.worktree_roots)
        self._gpu = gpu
        self._gates = gates
        self._launcher: GpuLauncher | None = (
            None if gpu is None else (gpu.launcher or AgentGpuLauncher(gpu.config))
        )
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
            if job.relay is not None:
                job.relay[0].unlink(missing_ok=True)

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
            except OSError:
                cancel.set()

        status = job.run(write, cancel)
        if cancel.is_set():
            return
        try:
            if job.relay is not None:
                _relay_file(connection, *job.relay)
            _send(connection, {"exit": status})
        except OSError:
            return

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
    "BrokerTransport",
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
