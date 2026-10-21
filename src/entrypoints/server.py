"""Serving entrypoint for VibeSys frontend clients."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import webbrowser
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol

from entrypoints import cli
from entrypoints.main_thread_interrupt import MainThreadInterrupt
from entrypoints.production import production_run_readers, production_runs, production_web_discovery
from entrypoints.web_assets import WebAssetBundle
from server.host_api import (
    RUN_EVENTS_FILE,
    DetachedLaunchErrorCode,
    DetachedLaunchFailure,
    FailureReason,
    InstanceStatus,
    LiveInstanceRecord,
)
from server.registry import (
    INSTANCES_DIRECTORY,
    READERS_DIRECTORY,
    FileInstanceStore,
    LiveRegistry,
    driving,
    instance_log_path,
    instance_root,
    instance_run_directory,
    instance_socket_path,
    new_instance_id,
)
from server.runtime import (
    READER_IDLE_SECONDS,
    WEBSOCKET_CLOSE_TIMEOUT_SECONDS,
    WebDiscovery,
    WebGatewayListener,
    WebInstanceClaim,
    WebInstanceHold,
    WebInstanceRecord,
    WebPortInspector,
    WebPortObservation,
    WebPortState,
    WebSocketBindError,
    browser_origin,
)
from vibesys.api import ConfigurationError
from vs_project.api import (
    Project,
    ProjectStateError,
    create_operator_key,
    user_runtime_root,
    validate_run_id,
)
from vs_sim.api import (
    OsThreads,
    Platform,
    SignalMainThreadWake,
    Signals,
    SystemClock,
    WakeupFdSignalRelay,
)

_WEB_PORT_MAX = 65_535
_DETACHED_START_HANG_GUARD_SECONDS = 600.0
"""How long a detached server that is still running may take to publish its record.

Readiness is the child's to announce, and a child that exits is seen at once, so
this bound never decides how long a start takes: it only ends a child that is alive
and will not publish. It is generous because the first start after an install
compiles bytecode and loads every module (tens of seconds on a slow disk), and
a guess near that time fails a healthy start.
"""
_DETACHED_STOP_TIMEOUT_SECONDS = 2.0
_GATEWAY_STOP_GRACE_SECONDS = 2.0
GATEWAY_STOP_TIMEOUT_SECONDS = WEBSOCKET_CLOSE_TIMEOUT_SECONDS + _GATEWAY_STOP_GRACE_SECONDS
"""Transport close bound plus grace to observe the released instance files."""
_DETACHED_POLL_SECONDS = 0.05
_DETACHED_LOG_TAIL_BYTES = 4_096

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import BinaryIO

    from server.runtime import ServerLifecycleObserver, ServerRuntime
    from vibesys.api import RunReader, RunReaders, Runs
    from vs_sim.api import MainThreadWake, SignalRelay, Threads


def _report_stuck_main_thread(waited: float) -> None:
    sys.stderr.write(
        f"vibesys: the run has not stopped {waited:.0f} s after SIGTERM; its main thread "
        "is not taking signals (blocked in a native call?). SIGKILL stops it.\n"
    )


@contextmanager
def _termination_signal(
    runtime: ServerRuntime, relay: SignalRelay, wake: MainThreadWake, threads: Threads
) -> Iterator[None]:
    """Forward the launcher's signals into the server's run and cleanup boundary.

    A Ctrl-C (SIGINT) stops the run through its control, cancelling its work in flight,
    as headless does; with no run to stop, it ends the server like SIGTERM. SIGTERM and
    SIGHUP end the server now, through its cleanup boundary. The relay owns SIGINT too,
    so ``asyncio.run`` installs no handler that would cancel the run's driver on the
    first SIGINT and raise inside its teardown on the next.

    The kernel may hand a process-directed signal to a transport thread, while
    CPython runs Python handlers on the main thread only, so a handler installed
    for SIGTERM is not run while the main thread is blocked in the run. *relay*
    observes the signal on whichever thread took it, then *wake* interrupts the main
    thread, resent until the scope ends so discarded interruptions can be recovered
    (see ``MainThreadInterrupt``).
    """

    def on_wake() -> None:
        # Bound below, before the relay that alone requests a wake is entered.
        interrupt.take()

    with ExitStack() as scope:
        send = scope.enter_context(wake.armed(on_wake))
        interrupt = MainThreadInterrupt(send, threads, _report_stuck_main_thread)
        scope.enter_context(interrupt.recovering())

        def on_signal(signum: int) -> None:
            number = Signals(signum)
            if number is Signals.SIGINT and runtime.interrupt():
                return
            runtime.shutdown(number)
            interrupt.request()

        scope.enter_context(
            relay.relay([Signals.SIGINT, Signals.SIGTERM, Signals.SIGHUP], on_signal)
        )
        # Keep the callback as a backstop if the first interruption lands in the
        # finally suite itself. Both disarm requests before the relay joins them.
        scope.callback(interrupt.finish)
        try:
            yield
        finally:
            interrupt.finish()


def _control_socket_from_argv(argv: list[str]) -> Path | None:
    """Read the transport bootstrap flag without parsing run configuration."""
    value = cli.option_from_argv(argv, "--control-socket")
    return Path(value) if value else None


def _refuse_control_socket_outside_runtime_root(path: Path) -> None:
    """Exit with a configuration error unless ``path``'s directory is in the runtime root.

    Every operator client presents the key only to a socket there (``OperatorDialer``),
    and a confined agent can write elsewhere, so a socket outside it is one the operator
    could not reach and an agent could take over.
    """
    root = user_runtime_root(os.environ, os.getuid())
    directory = path.absolute().parent.resolve()
    resolved_root = root.resolve()
    if directory != resolved_root and resolved_root not in directory.parents:
        cli.configuration_error(
            f"--control-socket {path} is not inside the operator's runtime root {root}: "
            "operator clients present the control key only to a socket there, where no "
            "confined agent can write. Choose a path inside it, or set XDG_RUNTIME_DIR",
            code="invalid_arguments",
            stage="argument_parsing",
        )


def _web_requested(argv: list[str]) -> bool:
    return "--web" in argv or "--web-reopen" in argv


def _detach_requested(argv: list[str]) -> bool:
    return "--detach" in argv


def _web_port_from_argv(argv: list[str]) -> int:
    value = cli._option_from_argv(argv, "--web-port")  # noqa: SLF001  # lint-waiver: LW-101001 [SLF001]; reuse the CLI's private option scanner before full argument parsing
    if value is None:
        return 0
    try:
        port = int(value)
    except ValueError:
        raise ValueError("--web-port must be an integer") from None  # noqa: TRY003  # lint-waiver: LW-101002 [TRY003]; report malformed launcher flags as user-facing configuration errors
    if not 0 <= port <= _WEB_PORT_MAX:
        raise ValueError("--web-port must be between 0 and 65535")  # noqa: TRY003  # lint-waiver: LW-101003 [TRY003]; report malformed launcher flags as user-facing configuration errors
    return port


def _web_assets_from_argv(argv: list[str]) -> WebAssetBundle | None:
    value = cli._option_from_argv(argv, "--web-assets")  # noqa: SLF001  # lint-waiver: LW-101004 [SLF001]; reuse the CLI's private option scanner before full argument parsing
    if value is not None:
        return WebAssetBundle.from_directory(Path(value), require_manifest=False)
    return None


def _web_origins_from_argv(argv: list[str]) -> tuple[str, ...]:
    origins: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--web-origin":
            if index + 1 >= len(argv):
                raise ValueError("--web-origin requires an origin")  # noqa: TRY003  # lint-waiver: LW-101068 [TRY003]; report a missing browser-origin value before gateway startup
            value = argv[index + 1]
            index += 2
        elif argument.startswith("--web-origin="):
            value = argument.partition("=")[2]
            index += 1
        else:
            index += 1
            continue
        # Validated against the gateway's own definition rather than a second
        # copy of it, so the operator gets a `configuration_error` naming the
        # flag and the value before any run setup starts, instead of the same
        # rejection from deep inside `ServerRuntime.run`.
        try:
            origin = browser_origin(value)
        except ValueError as error:
            raise ValueError(f"--web-origin {error}") from None  # noqa: TRY003  # lint-waiver: LW-101069 [TRY003]; prefix the gateway's rejection with the flag that carried the value, which a bare exception class cannot do
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


def _web_instance_from_argv(argv: list[str]) -> Path:
    value = cli._option_from_argv(argv, "--web-instance")  # noqa: SLF001  # lint-waiver: LW-101042 [SLF001]; reuse the CLI's private option scanner for the launcher-only flag
    return (
        Path(value).expanduser().resolve()
        if value is not None
        else (Project.open(Path.cwd()).configuration_path() / "web-gateway.json").resolve()
    )


def _first_option(argv: list[str], options: tuple[str, ...]) -> str | None:
    """The value of the first of ``options`` that ``argv`` sets."""
    for option in options:
        value = cli._option_from_argv(argv, option)  # noqa: SLF001  # lint-waiver: LW-101043 [SLF001]; reuse the CLI's private option scanner for the launcher-only flag
        if value is not None:
            return value
    return None


def _read_only_log_from_argv(argv: list[str]) -> Path | None:
    """The journal a read-only server serves: ``--web-reopen`` with the web UI, ``--reopen`` without."""
    value = _first_option(argv, ("--web-reopen", "--reopen"))
    if value is None:
        return None
    path = Path(value).expanduser().resolve()
    return path.parent if path.name == RUN_EVENTS_FILE else path


def _chat_reader_opener(
    arguments: list[str], run_readers: RunReaders
) -> Callable[[], RunReader] | None:
    """How the read-only server opens the run it serves for conversation.

    The request is the one resuming the run builds: its record restores the run's settings, so
    nothing is passed but the run's id. The server runs in the run's project directory. A server
    reopened without the run's id (a journal read for its events) has no chat agent.
    """
    run_id = _first_option(arguments, ("--run-id",))
    if run_id is None:
        return None

    def open_reader() -> RunReader:
        request = cli.build_run_request(cli.parse_cli_invocation(["--resume", run_id]))
        return run_readers(request)

    return open_reader


def _headless_argv(argv: list[str]) -> list[str]:
    """Remove server-only options before dispatching to the core CLI."""
    arguments: list[str] = []
    skip_next = False
    for argument in argv:
        if skip_next:
            skip_next = False
            continue
        if argument in {
            "--control-socket",
            "--web-port",
            "--web-assets",
            "--web-origin",
            "--web-instance",
            "--web-reopen",
            "--reopen",
            "--instance-id",
        }:
            skip_next = True
            continue
        if argument in {"--web", "--detach"}:
            continue
        if argument.startswith(
            (
                "--control-socket=",
                "--web-port=",
                "--web-assets=",
                "--web-origin=",
                "--web-instance=",
                "--web-reopen=",
                "--reopen=",
                "--instance-id=",
            )
        ):
            continue
        arguments.append(argument)
    return arguments


def _missing_control_socket() -> NoReturn:
    cli.configuration_error(
        "--control-socket is required by the frontend server",
        code="invalid_arguments",
        stage="argument_parsing",
    )


class _DetachedProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class _DetachedGatewayEffects:
    """Process, discovery, and clock effects for the detached gateway lifetime.

    One class covers launching and stopping because both act on the same
    gateway through the same mechanisms. `stop_detached_gateway` reaches it
    through the narrower `WebGatewayStopEffects` Protocol, which omits `spawn`
    and `discover`. Time passes on *threads*, and *discovery* finds the gateway
    and identifies processes; `real_gateway_effects` builds this host's.
    """

    def __init__(self, threads: Threads, discovery: WebDiscovery) -> None:
        self._threads = threads
        self._discovery = discovery

    def spawn(
        self,
        command: list[str],
        environment: dict[str, str],
        output: BinaryIO,
    ) -> _DetachedProcess:
        return subprocess.Popen(  # noqa: S603  # lint-waiver: LW-101035 [S603]; launch the detached child with a fixed interpreter/module command
            command,
            cwd=Path.cwd(),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def discover(self, instance_path: Path) -> WebInstanceRecord | None:
        return self._discovery.discover(instance_path, cleanup_stale=False)

    def read_record(self, instance_path: Path) -> WebInstanceRecord | None:
        return WebInstanceRecord.read(instance_path)

    def observe(self, instance_path: Path) -> WebInstanceHold:
        return WebInstanceHold.observe(instance_path)

    def terminate(self, pid: int) -> None:
        os.kill(pid, signal.SIGTERM)

    def inspect_port(self, port: int) -> WebPortObservation:
        return WebPortInspector(process_signaller=self._discovery.processes).inspect(port)

    def terminate_listener(self, listener: WebGatewayListener) -> None:
        WebPortInspector(process_signaller=self._discovery.processes).terminate(listener)

    def monotonic(self) -> float:
        return self._threads.now()

    def sleep(self, seconds: float) -> None:
        self._threads.sleep(seconds)


def real_gateway_effects() -> _DetachedGatewayEffects:
    """This host's gateway effects: OS threads and the production discovery; entrypoints only."""
    return _DetachedGatewayEffects(OsThreads(), production_web_discovery())


def _spawn_detached(
    arguments: list[str],
    instance_path: Path,
    effects: _DetachedGatewayEffects,
) -> None:
    """Start the long-lived child and wait for its capability record."""
    instance_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = WebInstanceHold.log_path(instance_path)
    descriptor = os.open(log_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w+b") as output:
        # The child inherits this descriptor as its stdout and stderr, so lock
        # and log are one open file description and the lock is released by the
        # *last* close of it, which is the child's exit rather than the
        # `output.close()` this block performs. Take it before truncating: if
        # another gateway still holds the log, its output must survive.
        if not WebInstanceHold.take(descriptor):
            raise RuntimeError(
                _instance_in_use(instance_path, WebInstanceHold.observe(instance_path))
            )
        os.fchmod(descriptor, 0o600)
        output.truncate(0)
        record = _start_detached_child(
            _DetachedLaunch(
                module="entrypoints.server",
                arguments=_detached_child_arguments(arguments, instance_path),
                output=output,
                log_path=log_path,
                ready=lambda: effects.discover(instance_path),
                name="Detached VibeSys web gateway",
            ),
            effects,
        )
        if _web_assets_from_argv(arguments) is not None:
            print(f"VibeSys web UI: {record.url}", flush=True)  # noqa: T201  # lint-waiver: LW-101036 [T201]; expose the detached capability URL to the launcher user
            webbrowser.open(record.url, new=2)
        else:
            print(f"VibeSys gateway: {record.url}", flush=True)  # noqa: T201  # lint-waiver: LW-101200 [T201]; expose the detached capability URL to the launcher user


@dataclass(frozen=True)
class _DetachedLaunch[T]:
    """One detached server to start: what to run, where its output goes, when it is ready."""

    module: str
    arguments: list[str]
    output: BinaryIO
    log_path: Path
    ready: Callable[[], T | None]
    """Polled until it returns the started server's record."""
    name: str
    """How failures name the server to an operator."""

    def exited(self, status: int) -> _DetachedStartError:
        return self._failure(f"{self.name} exited with status {status}")

    def not_ready(self) -> _DetachedStartError:
        """The server was still running, so it hung or was still loading, but it did not crash."""
        return self._failure(
            f"{self.name} did not become ready within {_DETACHED_START_HANG_GUARD_SECONDS:g} "
            "seconds and was stopped. It was still running, not crashed: it hung, or it "
            "was still loading"
        )

    def _failure(self, summary: str) -> _DetachedStartError:
        return _DetachedStartError(
            _detached_failure(summary, self.log_path, self.output), self.log_path
        )


def _start_detached_child[T](launch: _DetachedLaunch[T], effects: _DetachedGatewayEffects) -> T:
    """Spawn a detached server in its own session and wait, bounded, until it is ready.

    The child gets a new session (no controlling terminal, so a closed SSH
    session's hangup never reaches it), stdin from ``/dev/null``, and the
    launch's output as stdout and stderr.
    """
    environment = {**os.environ, "VIBESYS_DETACHED_CHILD": "1"}
    process = effects.spawn(
        [sys.executable, "-m", launch.module, *launch.arguments],
        environment,
        launch.output,
    )
    deadline = effects.monotonic() + _DETACHED_START_HANG_GUARD_SECONDS
    while effects.monotonic() < deadline:
        value = launch.ready()
        if value is not None:
            return value
        status = process.poll()
        if status is not None:
            raise launch.exited(status)
        effects.sleep(_DETACHED_POLL_SECONDS)
    # Say in the log itself what ended the start, so a log the server never wrote to
    # is not left blank. The child shares this file's offset, so append at its end.
    launch.output.seek(0, os.SEEK_END)
    launch.output.write(
        f"vibesys: launcher stopped this server: it published no record within "
        f"{_DETACHED_START_HANG_GUARD_SECONDS:g} s of starting\n".encode()
    )
    launch.output.flush()
    _stop_detached(process)
    raise launch.not_ready()


class _DetachedStartError(RuntimeError):
    """A detached server that exited or never became ready; names its log."""

    def __init__(self, message: str, log_path: Path) -> None:
        super().__init__(message)
        self.log_path = log_path


def _spawn_detached_instance(
    arguments: list[str],
    root: Path,
    effects: _DetachedGatewayEffects,
    *,
    registry_directory: str = INSTANCES_DIRECTORY,
) -> LiveInstanceRecord:
    """Start a headless detached server under ``root`` and return its serving record.

    ``arguments`` name the run (``--run-id``); the server publishes it in its
    first record, before it loads the engine (``entrypoints.detached_run``), in the
    registry ``registry_directory`` names.
    """
    registry = LiveRegistry(FileInstanceStore(root, directory=registry_directory), SystemClock())
    instance_id = new_instance_id()
    run_directory = instance_run_directory(root, instance_id)
    run_directory.parent.mkdir(mode=0o700, exist_ok=True)
    run_directory.mkdir(mode=0o700)
    log_path = instance_log_path(root, instance_id)
    descriptor = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w+b") as output:

        def serving() -> LiveInstanceRecord | None:
            record = registry.find(instance_id)
            return (
                record if record is not None and record.status is InstanceStatus.SERVING else None
            )

        return _start_detached_child(
            _DetachedLaunch(
                module="entrypoints.detached_run",
                arguments=[
                    *arguments,
                    "--control-socket",
                    str(instance_socket_path(root, instance_id)),
                    "--instance-id",
                    instance_id,
                ],
                output=output,
                log_path=log_path,
                ready=serving,
                name="Detached VibeSys server",
            ),
            effects,
        )


def _detached_child_arguments(arguments: list[str], instance_path: Path) -> list[str]:
    """Give the child one explicit, resolved instance identity for port recovery."""
    normalized: list[str] = []
    resolved_instance = str(instance_path.expanduser().resolve())
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--web-instance":
            index += 2
            continue
        if argument.startswith("--web-instance="):
            index += 1
            continue
        normalized.append(argument)
        index += 1
    normalized.extend(("--web-instance", resolved_instance))
    return normalized


def _instance_in_use(instance_path: Path, hold: WebInstanceHold) -> str:
    """Explain a refused launch, naming the processes in the way, if it can.

    The blocker is whatever holds the startup log, which need not be a VibeSys
    gateway: a descendant
    that inherited the log keeps it after the gateway is gone, and nothing
    publishes a record for such a process. So this names the measured holders
    and offers both escapes rather than routing every case through `stop`.
    """
    log_path = WebInstanceHold.log_path(instance_path)
    users = (
        f" Processes with files open under {instance_path.parent}: "
        f"{', '.join(str(pid) for pid in hold.holders)}."
        if hold.holders
        else ""
    )
    return (
        f"Another process holds {log_path}, so this launch would overwrite a running "
        f"gateway's output.{users} End those processes before starting another gateway."
    )


def _stop_detached(process: _DetachedProcess) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=_DETACHED_STOP_TIMEOUT_SECONDS)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=_DETACHED_STOP_TIMEOUT_SECONDS)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            return


class GatewayStopOutcome(StrEnum):
    """What `stop_detached_gateway` observed about one instance directory."""

    NOT_RUNNING = "not_running"
    """No record named a gateway and nothing observable was using the files."""
    STOPPED = "stopped"
    """The directory was in use, and no process this host can observe uses it now."""
    STILL_HOLDING = "still_holding"
    """The budget expired with the directory in use, or its state was unreadable."""


@dataclass(frozen=True)
class GatewayStopResult:
    """What one stop request observed, and which pid it signalled.

    ``hold`` is the last observation of the instance directory, which is what
    decided ``outcome``. A caller reporting a failure reads it to name the
    processes the operator has to deal with, because the instance record often
    does not name them. ``pid`` is the gateway this request actually delivered
    SIGTERM to, and is ``None`` when no record named a process it could signal.
    """

    outcome: GatewayStopOutcome
    hold: WebInstanceHold
    pid: int | None = None


class WebGatewayStopEffects(Protocol):
    """The process, filesystem, and clock effects a stop request needs."""

    def read_record(self, instance_path: Path) -> WebInstanceRecord | None:
        """Return the published instance record without probing the gateway."""
        ...

    def observe(self, instance_path: Path) -> WebInstanceHold:
        """Report which processes are observed to be using the instance files."""
        ...

    def terminate(self, pid: int) -> None:
        """Ask ``pid`` to shut down, raising if it is gone or not ours."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic reading used only to enforce the stop budget."""
        ...

    def sleep(self, seconds: float) -> None:
        """Wait before observing the instance directory again."""
        ...


def stop_detached_gateway(
    instance_path: Path,
    effects: WebGatewayStopEffects,
) -> GatewayStopResult:
    """Stop the gateway using ``instance_path`` and wait for its files to close.

    A `STOPPED` result means no process this host can observe is using the
    instance directory, so the caller may reuse or remove it. That is a
    statement about what `WebInstanceHold.observe` can see (a `/proc` scan plus
    the startup log's lock), not a proof that the directory is unused: a holder
    owned by another user is invisible to the scan, and a host that exposes
    neither witness cannot answer at all. Where the answer is unreadable the
    result is `STILL_HOLDING`, never `STOPPED`, so missing evidence is never
    reported as success. The postcondition is deliberately not "the record is
    gone" (the gateway unlinks it first) nor "the recorded pid is gone" (a
    descendant can still hold the inherited log).

    The record is read before deciding anything and re-read on every poll: it
    is the only thing that names a pid to signal, it may not exist yet when a
    stop races a launch, and a gateway launched without `--detach` never
    publishes one at all, which is why the observation and not the record
    decides whether there is something to stop. A recorded pid is signalled
    only once it is observed to hold a file under the directory, so a record
    left behind by a killed gateway cannot direct SIGTERM at whatever process
    has since been assigned that pid. Where descriptors are unobservable there
    is nothing to validate against, and the recorded pid is signalled unchecked
    rather than leaving the operator no way to stop a gateway.

    No health probe is used. A probe answers whether the gateway can still
    serve a browser, which is false throughout teardown and load-sensitive;
    stopping only needs to know which process published the instance.

    Escalation is the operator's, not this function's: it sends SIGTERM and
    never SIGKILL, because SIGTERM is what runs the gateway's ordered teardown
    (release the record, close the transport, close the session) and a killed
    gateway leaves its record behind for the next launch to trip over. A signal
    that raises `ProcessLookupError` or `PermissionError` was never delivered,
    so the result does not claim it was, and the observation still decides the
    outcome.
    """
    hold = effects.observe(instance_path)
    record = effects.read_record(instance_path)
    if record is None and hold.free:
        return GatewayStopResult(GatewayStopOutcome.NOT_RUNNING, hold)
    deadline = effects.monotonic() + GATEWAY_STOP_TIMEOUT_SECONDS
    signalled: int | None = None
    addressed: set[int] = set()
    while True:
        if record is not None and record.pid not in addressed and _is_a_holder(hold, record.pid):
            addressed.add(record.pid)
            if _terminated(effects, record.pid):
                signalled = record.pid
        if hold.free:
            return GatewayStopResult(GatewayStopOutcome.STOPPED, hold, signalled)
        if effects.monotonic() >= deadline:
            return GatewayStopResult(GatewayStopOutcome.STILL_HOLDING, hold, signalled)
        effects.sleep(_DETACHED_POLL_SECONDS)
        hold = effects.observe(instance_path)
        record = effects.read_record(instance_path)


def _is_a_holder(hold: WebInstanceHold, pid: int) -> bool:
    """Report that ``pid`` holds the instance files, or that this host cannot tell."""
    return hold.holders is None or pid in hold.holders


def _terminated(effects: WebGatewayStopEffects, pid: int) -> bool:
    """Deliver SIGTERM to ``pid``, reporting whether it actually arrived."""
    try:
        effects.terminate(pid)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class GatewayPortStopOutcome(StrEnum):
    """What a port-keyed stop safely established."""

    NOT_RUNNING = "not_running"
    STOPPED = "stopped"
    REFUSED = "refused"
    STILL_HOLDING = "still_holding"


@dataclass(frozen=True)
class GatewayPortStopResult:
    """The final port observation and the verified gateway signalled, if any."""

    outcome: GatewayPortStopOutcome
    observation: WebPortObservation
    pid: int | None = None


class WebGatewayPortEffects(Protocol):
    """The discovery, signalling, and clock effects of a port-keyed stop."""

    def inspect_port(self, port: int) -> WebPortObservation:
        """Return a fresh, fail-closed observation of ``port``."""
        ...

    def terminate_listener(self, listener: WebGatewayListener) -> None:
        """Signal ``listener`` only if its complete identity still matches."""
        ...

    def monotonic(self) -> float:
        """Return the clock used to bound shutdown."""
        ...

    def sleep(self, seconds: float) -> None:
        """Wait before the next observation."""
        ...


def stop_web_gateway_on_port(
    port: int,
    effects: WebGatewayPortEffects,
) -> GatewayPortStopResult:
    """Stop only a listener proven to be the same-user VibeSys gateway."""
    observation = effects.inspect_port(port)
    if observation.state is WebPortState.FREE:
        return GatewayPortStopResult(GatewayPortStopOutcome.NOT_RUNNING, observation)
    listener = observation.gateway
    if observation.state is not WebPortState.VIBESYS_GATEWAY or listener is None:
        return GatewayPortStopResult(GatewayPortStopOutcome.REFUSED, observation)
    try:
        effects.terminate_listener(listener)
    except (NotImplementedError, OSError):
        observation = effects.inspect_port(port)
        outcome = (
            GatewayPortStopOutcome.STOPPED
            if observation.state is WebPortState.FREE
            else GatewayPortStopOutcome.REFUSED
        )
        return GatewayPortStopResult(outcome, observation)
    deadline = effects.monotonic() + GATEWAY_STOP_TIMEOUT_SECONDS
    while True:
        observation = effects.inspect_port(port)
        if observation.state is WebPortState.FREE:
            return GatewayPortStopResult(
                GatewayPortStopOutcome.STOPPED,
                observation,
                listener.pid,
            )
        if observation.gateway != listener:
            return GatewayPortStopResult(
                GatewayPortStopOutcome.REFUSED,
                observation,
                listener.pid,
            )
        if effects.monotonic() >= deadline:
            return GatewayPortStopResult(
                GatewayPortStopOutcome.STILL_HOLDING,
                observation,
                listener.pid,
            )
        effects.sleep(_DETACHED_POLL_SECONDS)


def _detached_failure(summary: str, log_path: Path, output: BinaryIO) -> str:
    output.flush()
    length = output.seek(0, os.SEEK_END)
    output.seek(max(0, length - _DETACHED_LOG_TAIL_BYTES))
    tail = output.read().decode("utf-8", errors="replace").strip()
    message = f"{summary}. Startup log: {log_path}"
    if not tail:
        message += " (the server wrote nothing to it before it ended)"
    return f"{tail}\n{message}" if tail else message


class WebInstanceFinder(Protocol):
    """Finds the live gateway that an instance record names."""

    def discover(self, path: Path, *, cleanup_stale: bool = True) -> WebInstanceRecord | None:
        """Return the record at *path* when its gateway answers, else None."""
        ...


def _discover_web_instance(
    path: Path, finder: WebInstanceFinder, threads: Threads
) -> WebInstanceRecord | None:
    """Wait through the bind-to-record race, on *threads*' clock, before deciding to launch again."""
    clock = threads
    deadline = clock.now() + 2
    while True:
        record = finder.discover(path, cleanup_stale=False)
        if record is not None:
            return record
        if not WebInstanceClaim.is_held(path):
            return finder.discover(path)
        if clock.now() >= deadline:
            return finder.discover(path)
        clock.sleep(0.05)


def _launch_detached_instance(arguments: list[str], effects: _DetachedGatewayEffects) -> None:
    """Validate, spawn a headless detached server, and print one JSON line.

    The line is the server's ``LiveInstanceRecord`` on success (exit 0), else
    a ``DetachedLaunchFailure`` (exit nonzero, its ``exit_code``) with the same
    message, for a person, on stderr. ``--resume`` starts the stopped or
    crashed run under this process's interpreter, so a detached resume runs
    whatever code the invoked ``vibesys`` command points at.
    """
    try:
        record = _start_detached_instance(arguments, effects)
    except ConfigurationError as exc:
        diagnostic = exc.diagnostic
        failure = DetachedLaunchFailure(
            code=diagnostic.code,
            stage=diagnostic.stage,
            message=diagnostic.message,
            exit_code=diagnostic.exit_code,
        )
    except _DetachedLiveRunError as exc:
        failure = DetachedLaunchFailure(
            code=DetachedLaunchErrorCode.RUN_ALREADY_LIVE,
            stage="resume_resolution",
            message=str(exc),
            exit_code=1,
            live_instance=exc.record,
        )
    except PermissionError as exc:
        failure = DetachedLaunchFailure(
            code=DetachedLaunchErrorCode.REGISTRY_UNAVAILABLE,
            stage="registry",
            message=str(exc),
            exit_code=1,
        )
    except _DetachedStartError as exc:
        failure = DetachedLaunchFailure(
            code=DetachedLaunchErrorCode.SERVER_START_FAILED,
            stage="server_start",
            message=str(exc),
            exit_code=1,
            log_path=str(exc.log_path),
        )
    else:
        sys.stdout.write(record.model_dump_json() + "\n")
        return
    sys.stdout.write(failure.model_dump_json() + "\n")
    sys.stderr.write(f"vibesys: {failure.message}\n")
    raise SystemExit(failure.exit_code)


class _DetachedLiveRunError(RuntimeError):
    """``--resume`` named a run another live detached server is driving."""

    def __init__(self, record: LiveInstanceRecord) -> None:
        super().__init__(
            f"run {record.run_id!r} is already live in detached server {record.id}; "
            "attach to it, or stop it first with `vibesys instances stop "
            f"{record.id}`"
        )
        self.record = record


def _start_detached_instance(
    arguments: list[str], effects: _DetachedGatewayEffects
) -> LiveInstanceRecord:
    """Reject a malformed or already-live run, then spawn it; raise on any failure.

    The run's id is fixed here, before anything detaches: parsing allocates a
    fresh run's id and resolves a resume (``latest`` included) to the run it
    names. The server receives it as ``--run-id`` and names that run in its
    record and in every event from the first.
    """
    if _control_socket_from_argv(arguments) is not None:
        cli.configuration_error(
            "--detach without --web chooses its own control socket; drop --control-socket",
            code="invalid_arguments",
            stage="argument_parsing",
        )
    root = instance_root(os.environ, os.getuid())
    if _read_only_log_from_argv(arguments) is not None:
        return _start_detached_reader(arguments, root, effects)
    # Reject a malformed run before anything is spawned, so the operator sees
    # the diagnostic here rather than in a detached server's log.
    run_id: str = cli.parse_cli_invocation(_headless_argv(arguments)).args.run_id
    live = driving(LiveRegistry(FileInstanceStore(root), SystemClock()).list(), run_id)
    if live is not None:
        raise _DetachedLiveRunError(live)
    return _spawn_detached_instance([*arguments, "--run-id", run_id], root, effects)


def _start_detached_reader(
    arguments: list[str], root: Path, effects: _DetachedGatewayEffects
) -> LiveInstanceRecord:
    """Spawn the read-only server of the journal ``--reopen`` names, registered as a reader.

    The run is not started, so there is no run request to parse: ``--run-id`` names the run
    whose recorded journal is served. A reader drives nothing, so a live server of the same run
    does not stop one from starting (it registers apart from the runs).
    """
    value = _first_option(arguments, ("--run-id",))
    if value is None:
        cli.configuration_error(
            "--reopen needs --run-id, the run the journal records",
            code="invalid_arguments",
            stage="argument_parsing",
        )
    try:
        validate_run_id(value)
    except ProjectStateError as exc:
        cli.configuration_error(str(exc), code="invalid_arguments", stage="argument_parsing")
    return _spawn_detached_instance(arguments, root, effects, registry_directory=READERS_DIRECTORY)


def main(
    argv: list[str] | None = None,
    *,
    observer: ServerLifecycleObserver | None = None,
    runs: Runs | None = None,
    run_readers: RunReaders | None = None,
) -> None:
    """Run the frontend server and headless engine in one process.

    ``observer`` is a detached server's registry record
    (``entrypoints.detached_run``), told when the server reaches a milestone.
    ``runs`` and ``run_readers`` replace the application-wired run service and finished-run
    opener in tests.

    Every configuration diagnostic raised below this frame is rendered to
    stderr and exits with the diagnostic's code, the way
    `entrypoints.headless.main` does. One handler here rather than one per
    raising site is what makes that total: argument parsing and `RunRequest`
    building both run before any transport binds, so a diagnostic they raise
    has no structured channel to reach a frontend on, and stderr is inherited
    by the launcher and redirected onto the detached startup log.
    """
    cli.start_process()
    try:
        _serve(sys.argv[1:] if argv is None else argv, observer, runs, run_readers)
    except ConfigurationError as exc:
        if observer is not None:
            diagnostic = exc.diagnostic
            observer.observe(
                failure=FailureReason(
                    code=diagnostic.code, stage=diagnostic.stage, message=diagnostic.message
                )
            )
        cli.render_configuration_error(exc)
    except WebSocketBindError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.stderr.write(f"Inspect or stop the listener on port {exc.port} before retrying.\n")
        raise SystemExit(1) from None


def _serve(  # noqa: C901, PLR0912, PLR0915  # lint-waiver: LW-101031 [C901, PLR0912, PLR0915]; the entrypoint owns ordered setup, parsing, execution, and cleanup branches
    arguments: list[str],
    observer: ServerLifecycleObserver | None,
    runs: Runs | None,
    run_readers: RunReaders | None,
) -> None:
    """Serve one invocation, leaving configuration diagnostics to `main`."""
    web = _web_requested(arguments)
    detach = _detach_requested(arguments)
    detached_child = os.environ.get("VIBESYS_DETACHED_CHILD") == "1"
    if detach and not web and not detached_child:
        _launch_detached_instance(arguments, real_gateway_effects())
        return
    try:
        # Resolve the browser artifact before reusing or spawning a gateway.
        # A detached parent otherwise bypasses the child's validation when an
        # old instance is already running and silently reports its URL.
        web_bundle = _web_assets_from_argv(arguments) if web else None
    except ValueError as exc:
        cli.configuration_error(
            str(exc),
            code="invalid_arguments",
            stage="argument_parsing",
        )
    instance_path = _web_instance_from_argv(arguments) if web else None
    if web and not detached_child:
        if instance_path is None:  # pragma: no cover - web always supplies a path.
            raise RuntimeError("Web instance path was not resolved")  # noqa: TRY003  # lint-waiver: LW-101038 [TRY003]; guard an impossible parser/launcher invariant
        existing = _discover_web_instance(instance_path, production_web_discovery(), OsThreads())
        if existing is not None:
            if web_bundle is not None and existing.web_build_id != web_bundle.build_id:
                cli.configuration_error(
                    f"running gateway {instance_path} records web build "
                    f"{existing.web_build_id or 'unknown'}, but {web_bundle.directory} is now "
                    f"{web_bundle.build_id}; stop that gateway and retry",
                    code="invalid_arguments",
                    stage="argument_parsing",
                )
            if web_bundle is not None:
                print(f"VibeSys web UI: {existing.url}", flush=True)  # noqa: T201  # lint-waiver: LW-101039 [T201]; expose the reused capability URL to the launcher user
                webbrowser.open(existing.url, new=2)
            else:
                print(f"VibeSys gateway: {existing.url}", flush=True)  # noqa: T201  # lint-waiver: LW-101201 [T201]; expose the reused capability URL to the launcher user
            return
        if detach:
            try:
                _spawn_detached(arguments, instance_path, real_gateway_effects())
            except RuntimeError as exc:
                # A refused or failed detached launch is an operator-facing
                # outcome, not a defect, so it reports a message rather than a
                # traceback. `_spawn_detached` has already released the log.
                sys.stderr.write(f"{exc}\n")
                raise SystemExit(1) from None
            return
    control_socket = _control_socket_from_argv(arguments)
    if control_socket is None and not web:
        _missing_control_socket()
    # Validate launcher-only values before allocating the ephemeral socket, so
    # every rejected invocation remains free of resources that need cleanup.
    try:
        web_port = _web_port_from_argv(arguments)
        web_origins = _web_origins_from_argv(arguments)
        read_only_log = _read_only_log_from_argv(arguments)
    except ValueError as exc:
        cli.configuration_error(
            str(exc),
            code="invalid_arguments",
            stage="argument_parsing",
        )
    if control_socket is not None:
        _refuse_control_socket_outside_runtime_root(control_socket)
    temp_socket_dir: tempfile.TemporaryDirectory[str] | None = None
    if control_socket is None:
        # In the operator's runtime root, where no confined agent can write: in a shared
        # /tmp an agent could move the socket aside and bind its own to read the operator
        # key a client presents. Clients refuse a socket anywhere else (OperatorDialer).
        temp_socket_dir = tempfile.TemporaryDirectory(
            prefix="vibesys-web-", dir=instance_root(os.environ, os.getuid())
        )
        control_socket = Path(temp_socket_dir.name) / "control.sock"
    try:
        request = (
            None
            if read_only_log is not None
            else cli.build_run_request(cli.parse_cli_invocation(_headless_argv(arguments)))
        )
        if observer is not None and request is not None:
            observer.resolved(
                project_root=request.project_root,
                task=request.input_bundle.task_name,
            )
        run_id = None if request is None else request.resolved_run_id
        project_root = None if request is None else request.resolved_project_root
        server_runtime = import_module("server.runtime").ServerRuntime
        open_reader = (
            None
            if read_only_log is None
            else _chat_reader_opener(
                arguments, production_run_readers() if run_readers is None else run_readers
            )
        )
        platform = Platform.real()
        control_key = create_operator_key(os.environ, os.getuid())
        if web:
            runtime = server_runtime(
                socket_path=control_socket,
                control_key=control_key,
                runs=production_runs() if runs is None else runs,
                run_id=run_id,
                project_root=project_root,
                web=True,
                web_port=web_port,
                web_assets=web_bundle.directory if web_bundle is not None else None,
                web_build_id=web_bundle.build_id if web_bundle is not None else None,
                web_origins=web_origins,
                instance_path=instance_path,
                detach=detach,
                read_only_log=read_only_log,
                open_reader=open_reader,
                platform=platform,
            )
        else:
            runtime = server_runtime(
                socket_path=control_socket,
                control_key=control_key,
                runs=production_runs() if runs is None else runs,
                run_id=run_id,
                project_root=project_root,
                detach=detach,
                read_only_log=read_only_log,
                open_reader=open_reader,
                reader_idle_seconds=READER_IDLE_SECONDS if read_only_log is not None else None,
                observer=observer,
                platform=platform,
            )
        threads = platform.threads
        with _termination_signal(
            runtime, WakeupFdSignalRelay(threads), SignalMainThreadWake(), threads
        ):
            result = runtime.run(lambda: None if request is None else runtime.drive(request))
    finally:
        if temp_socket_dir is not None:
            temp_socket_dir.cleanup()
    # `result` is `None` when `ServerRuntime.run` absorbed an operator stop
    # (`RunStopped`) as a clean backend exit; only a completed run's
    # `RunResult.succeeded` decides the process exit code.
    if result is not None and not result.succeeded and result.status.value != "parked":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
