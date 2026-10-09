"""In-process Docker daemon for running a real ``DockerSandbox`` without Docker."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sandbox.container_runtime import (
    NESTED_DAEMON_LOG,
    NESTED_DAEMON_READY_SCRIPT,
    NESTED_DAEMON_START_SCRIPT,
)
from vs_sandbox.docker_cli import EXEC_MARKER_ENV, SIGNAL_EXEC_SCRIPT
from vs_sandbox.process_execution import shell_exit_status, start_process_group

if TYPE_CHECKING:
    from collections.abc import Sequence

# The container program's parent, standing in for the ``docker exec`` client.
# It starts the program in a session of its own (the daemon's side), records
# the program's pid so a later signal request can find it, and relays its
# output. Killing this client leaves the program running, as killing the real
# ``docker exec`` client does; only a signal request through the daemon
# reaches the program.
_EXEC_CLIENT = r"""
import json, os, signal, subprocess, sys, threading
spec = json.loads(sys.argv[1])
STOPS = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}

def register():
    signal.pthread_sigmask(signal.SIG_UNBLOCK, STOPS)
    # Runs in the new session before the program starts, so the program is findable
    # by a signal request from the moment it exists, as in the daemon.
    tmp = spec["pidfile"] + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(str(os.getpid()))
    os.replace(tmp, spec["pidfile"])

# Starting the program is atomic with respect to a stop: the daemon never half-creates
# an exec, so a stop aimed at this client waits until the program exists and is findable.
signal.pthread_sigmask(signal.SIG_BLOCK, STOPS)
child = subprocess.Popen(
    spec["argv"], cwd=spec["cwd"], env=spec["env"], stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    preexec_fn=register,
)
signal.pthread_sigmask(signal.SIG_UNBLOCK, STOPS)

def pump(source, sink):
    for chunk in iter(lambda: source.read1(65536), b""):
        sink.write(chunk)
        sink.flush()

pumps = [
    threading.Thread(target=pump, args=(child.stdout, sys.stdout.buffer)),
    threading.Thread(target=pump, args=(child.stderr, sys.stderr.buffer)),
]
for thread in pumps:
    thread.start()
status = child.wait()
for thread in pumps:
    thread.join()
sys.exit(128 - status if status < 0 else status)
"""

_FLAGS_WITH_VALUE = frozenset({"-e", "-w", "-u", "--env", "--workdir", "--user"})
_FLAGS = frozenset({"-i", "-t", "-it", "-d"})
_AGENT_IDENTITY_SCRIPT = "id -u agent && id -g agent"
_DAEMON_ERROR_EXIT = 1
_RUN_ERROR_EXIT = 125
_DEFAULT_RUNTIMES = ("runc",)


@dataclass(frozen=True, slots=True)
class FakeContainer:
    """What ``docker ps -a`` shows of one container the daemon still holds."""

    container_id: str
    name: str
    labels: dict[str, str]
    running: bool


@dataclass(slots=True)
class _Container:
    mounts: dict[str, Path]
    workdir: str
    name: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    runtime: str | None = None
    running: bool = True
    nested_daemon_started: bool = False
    markers: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def _run_env(arguments: tuple[str, ...]) -> dict[str, str]:
    """Return the ``-e KEY=VALUE`` variables of a ``docker run``, which every exec inherits."""
    return {
        key: value
        for index, argument in enumerate(arguments[:-1])
        if argument == "-e"
        for key, _, value in [arguments[index + 1].partition("=")]
    }


@dataclass(frozen=True, slots=True)
class _Exec:
    container: str
    env: dict[str, str]
    workdir: str | None
    user: str | None
    program: tuple[str, ...]


def _parse_exec(argv: tuple[str, ...]) -> _Exec:
    env: dict[str, str] = {}
    workdir: str | None = None
    user: str | None = None
    index = 2
    while argv[index].startswith("-"):
        flag = argv[index]
        if flag in _FLAGS_WITH_VALUE:
            value = argv[index + 1]
            if flag in {"-e", "--env"}:
                name, _, text = value.partition("=")
                env[name] = text
            elif flag in {"-w", "--workdir"}:
                workdir = value
            elif flag in {"-u", "--user"}:
                user = value
            index += 2
        elif flag in _FLAGS:
            index += 1
        else:
            message = f"unsupported docker exec flag {flag!r}"
            raise AssertionError(message)
    return _Exec(argv[index], env, workdir, user, tuple(argv[index + 1 :]))


class FakeDockerEngine:
    """A :class:`~vs_sandbox.docker_cli.DockerCli` backed by local processes.

    Models the part of a Docker daemon that a sandbox's command execution
    depends on, with a container's filesystem being the host directories its
    ``-v`` mounts name:

    * ``docker run -d`` registers a container; ``stop`` and ``rm`` end it, and
      every later ``exec`` fails the way the daemon does (exit 1, the daemon
      error on stderr).
    * ``docker exec`` runs its program locally, with the container's own
      ``docker run -e`` variables and the exec's ``-e`` variables as
      environment and ``-w`` (a container path) resolved through the mounts.
      Its exit status is the program's, a signalled program reporting
      ``128 + N``.
    * A spawned ``exec`` runs its program in a session of its own behind a
      relay that stands in for the ``docker exec`` client: killing the client
      does *not* stop the program. Only the stop script
      (:data:`~vs_sandbox.docker_cli.SIGNAL_EXEC_SCRIPT`), the way a sandbox
      signals processes inside a real container, or ``docker stop``/``rm``
      does. A sandbox that stops only its client leaks the program here, as it
      does against Docker.

    Not modelled: container-absolute paths inside command text (only ``-w``
    and the relative paths of the working directory resolve), users, images,
    and process isolation (``exec -u root`` setup scripts are recorded, not run). The real-daemon variant of the sandbox contract
    (``VIBESYS_E2E_DOCKER=1``) is the check on these approximations.

    Runtimes and nested daemons are modelled at the level a sandbox observes:
    ``docker info`` lists the runtimes the daemon registers, ``docker run
    --runtime X`` fails the way the daemon does for an unregistered ``X``, and
    a container's inner daemon answers its readiness check exactly when the
    sandbox started it with the daemon-start script and the engine was built
    to let nested daemons start.
    """

    def __init__(
        self,
        state_dir: Path | None = None,
        *,
        agent_ids: tuple[int, int] = (1000, 1000),
        runtimes: Sequence[str] = _DEFAULT_RUNTIMES,
        nested_daemons_start: bool = True,
    ) -> None:
        """Keep exec bookkeeping in *state_dir*; the ``agent`` user has *agent_ids*.

        *runtimes* are the names ``docker info`` reports; *nested_daemons_start*
        says whether a daemon started inside a container ever becomes ready.
        Without a *state_dir* the bookkeeping lives in a temporary directory
        that goes with the engine.
        """
        self._scratch: tempfile.TemporaryDirectory[str] | None = None
        if state_dir is None:
            self._scratch = tempfile.TemporaryDirectory()
            state_dir = Path(self._scratch.name)
        self._state_dir = state_dir
        self._agent_ids = agent_ids
        self._runtimes = tuple(runtimes)
        self._nested_daemons_start = nested_daemons_start
        self._containers: dict[str, _Container] = {}
        self.calls: list[tuple[str, ...]] = []
        self._signal_requests_finding_nothing = 0
        self._runs_lost_after_creating: list[bool] = []

    def containers(self) -> tuple[FakeContainer, ...]:
        """Every container the daemon still holds, stopped ones included."""
        return tuple(
            FakeContainer(identifier, container.name, dict(container.labels), container.running)
            for identifier, container in self._containers.items()
        )

    def runs_lost_after_creating(self, outcomes: Sequence[bool]) -> None:
        """Fault: each next ``docker run`` creates its container, then the client is lost.

        One entry per upcoming ``run``: ``False`` ends the client with an error
        and no container id on stdout (an interrupted or crashed client);
        ``True`` makes it raise ``subprocess.TimeoutExpired`` (a client that
        gave up waiting). The daemon has created the container either way.
        """
        self._runs_lost_after_creating = list(outcomes)

    def signal_requests_find_nothing(self, count: int) -> None:
        """Fault: the next *count* signal requests reach no process.

        This is what a request sees when the program has not started yet in the
        daemon when the request is handled.
        """
        self._signal_requests_finding_nothing = count

    def run(
        self, argv: Sequence[str], *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        """Run one ``docker`` subcommand to completion."""
        arguments = tuple(argv)
        self.calls.append(arguments)
        match arguments[1]:
            case "run":
                return self._create(arguments)
            case "exec":
                return self._exec(arguments, timeout_seconds)
            case "stop" | "rm":
                return self._end(arguments)
            case "ps":
                return self._list(arguments)
            case "info":
                runtimes = {name: {"path": name} for name in self._runtimes}
                return subprocess.CompletedProcess(arguments, 0, json.dumps(runtimes) + "\n", "")
            case other:
                message = f"FakeDockerEngine does not model `docker {other}`"
                raise AssertionError(message)

    def spawn(self, argv: Sequence[str]) -> subprocess.Popen[str]:
        """Start a ``docker exec`` whose program survives its client's death."""
        arguments = tuple(argv)
        self.calls.append(arguments)
        request = _parse_exec(arguments)
        container = self._containers.get(request.container)
        if container is None or not container.running:
            program: tuple[str, ...] = (
                "sh",
                "-c",
                f"echo 'Error response from daemon: No such container: {request.container}' >&2;"
                f" exit {_DAEMON_ERROR_EXIT}",
            )
            pidfile = self._state_dir / "unused.pid"
        else:
            program = request.program
            marker = request.env.get(EXEC_MARKER_ENV, f"anonymous-{uuid.uuid4().hex}")
            pidfile = self._pidfile(marker)
            container.markers.append(marker)
        spec = {
            "argv": list(program),
            "cwd": str(self._host_path(container, request.workdir)),
            "env": {**os.environ, **(container.env if container else {}), **request.env},
            "pidfile": str(pidfile),
        }
        return start_process_group(
            (sys.executable, "-c", _EXEC_CLIENT, json.dumps(spec)), env=None, cwd=None
        )

    def _pidfile(self, marker: str) -> Path:
        return self._state_dir / f"{marker}.pid"

    def _host_path(self, container: _Container | None, workdir: str | None) -> Path:
        if container is None:
            return self._state_dir
        target = workdir or container.workdir
        best = max(
            (prefix for prefix in container.mounts if target.startswith(prefix)),
            key=len,
            default=None,
        )
        if best is None:
            return self._state_dir
        return container.mounts[best] / target[len(best) :].lstrip("/")

    def _create(self, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        mounts: dict[str, Path] = {}
        labels: dict[str, str] = {}
        workdir = "/"
        runtime: str | None = None
        name = ""
        env = _run_env(arguments)
        for index, argument in enumerate(arguments):
            if argument == "--name":
                name = arguments[index + 1]
            elif argument == "--label":
                key, _, value = arguments[index + 1].partition("=")
                labels[key] = value
            elif argument == "-v":
                host, container, *_ = arguments[index + 1].split(":")
                mounts[container] = Path(host)
            elif argument == "--workdir":
                workdir = arguments[index + 1]
            elif argument == "--runtime":
                runtime = arguments[index + 1]
        if runtime is not None and runtime not in self._runtimes:
            stderr = (
                f"docker: Error response from daemon: unknown or invalid runtime name: {runtime}.\n"
            )
            return subprocess.CompletedProcess(arguments, _RUN_ERROR_EXIT, "", stderr)
        identifier = f"fake{len(self._containers):04d}{uuid.uuid4().hex[:8]}"
        self._containers[identifier] = _Container(mounts, workdir, name, labels, runtime, env=env)
        if self._runs_lost_after_creating:
            if self._runs_lost_after_creating.pop(0):
                raise subprocess.TimeoutExpired(arguments, 0)
            return subprocess.CompletedProcess(arguments, 130, "", "")
        return subprocess.CompletedProcess(arguments, 0, f"{identifier}\n", "")

    def _end(self, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        reference = arguments[-1]
        identifier = next(
            (
                known
                for known, candidate in self._containers.items()
                if reference in {known, candidate.name}
            ),
            reference,
        )
        container = self._containers.get(identifier)
        if container is None:
            return self._no_such_container(arguments, reference)
        for marker in container.markers:
            self._signal_marker(marker, signal.SIGKILL)
        container.running = False
        if arguments[1] == "rm":
            del self._containers[identifier]
        return subprocess.CompletedProcess(arguments, 0, f"{identifier}\n", "")

    def _list(self, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        """``docker ps [-a] -q [--filter label=K=V]...``: the matching container ids."""
        everything = any(
            a.startswith("-") and not a.startswith("--") and "a" in a for a in arguments
        )
        wanted: dict[str, str] = {}
        for flag, value in pairwise(arguments):
            if flag == "--filter" and value.startswith("label="):
                key, _, label = value.removeprefix("label=").partition("=")
                wanted[key] = label
        found = [
            identifier
            for identifier, container in self._containers.items()
            if (everything or container.running)
            and all(container.labels.get(key) == label for key, label in wanted.items())
        ]
        return subprocess.CompletedProcess(arguments, 0, "".join(f"{i}\n" for i in found), "")

    def _exec(
        self, arguments: tuple[str, ...], timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        request = _parse_exec(arguments)
        container = self._containers.get(request.container)
        if container is None or not container.running:
            return self._no_such_container(arguments, request.container)
        nested = self._nested_daemon_exec(container, request, arguments)
        if nested is not None:
            return nested
        if request.user == "root":
            # Image user administration (usermod, chown) is recorded in
            # `calls` but not run: this fake has no users.
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if request.program == ("sh", "-c", _AGENT_IDENTITY_SCRIPT):
            uid, gid = self._agent_ids
            return subprocess.CompletedProcess(arguments, 0, f"{uid}\n{gid}\n", "")
        if request.program[:3] == ("sh", "-c", SIGNAL_EXEC_SCRIPT):
            marker, number = request.program[4], request.program[5]
            if self._signal_requests_finding_nothing > 0:
                self._signal_requests_finding_nothing -= 1
            else:
                self._signal_marker(marker.partition("=")[2], signal.Signals(int(number)))
            return subprocess.CompletedProcess(arguments, 0, "", "")
        completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-731012 [S603]; the fake daemon runs the exec argv a sandbox assembled, without a shell.
            request.program,
            cwd=self._host_path(container, request.workdir),
            env={**os.environ, **container.env, **request.env},
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        return subprocess.CompletedProcess(
            arguments, shell_exit_status(completed.returncode), completed.stdout, completed.stderr
        )

    def _nested_daemon_exec(
        self, container: _Container, request: _Exec, arguments: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str] | None:
        """Answer the sandbox's nested-daemon start and readiness scripts, else ``None``."""
        if request.user != "root" or request.program[:2] != ("sh", "-c"):
            return None
        if request.program[2:] == (NESTED_DAEMON_START_SCRIPT,):
            container.nested_daemon_started = self._nested_daemons_start
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if request.program[2:3] == (NESTED_DAEMON_READY_SCRIPT,):
            if container.nested_daemon_started:
                return subprocess.CompletedProcess(arguments, 0, "", "")
            stderr = (
                f"nested Docker daemon not ready after {request.program[4]}s\n"
                f"(see {NESTED_DAEMON_LOG})\n"
            )
            return subprocess.CompletedProcess(arguments, _DAEMON_ERROR_EXIT, "", stderr)
        return None

    def _signal_marker(self, marker: str, number: signal.Signals) -> None:
        pidfile = self._pidfile(marker)
        if not pidfile.exists():
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(int(pidfile.read_text()), number)

    def runtime_of(self, container_id: str) -> str | None:
        """Return the ``--runtime`` *container_id* was created with, if any."""
        return self._containers[container_id].runtime

    def nested_daemon_running(self, container_id: str) -> bool:
        """Report whether a daemon started inside *container_id* is up."""
        container = self._containers.get(container_id)
        return container is not None and container.running and container.nested_daemon_started

    @staticmethod
    def _no_such_container(
        arguments: tuple[str, ...], identifier: str
    ) -> subprocess.CompletedProcess[str]:
        stderr = f"Error response from daemon: No such container: {identifier}\n"
        return subprocess.CompletedProcess(arguments, _DAEMON_ERROR_EXIT, "", stderr)
