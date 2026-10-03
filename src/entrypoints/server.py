"""Serving entrypoint for VibeSys frontend clients."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol

from entrypoints import cli
from server.runtime import WebInstanceClaim, WebInstanceHold, WebInstanceRecord, browser_origin
from server.settings import InteractiveSetupDefaults, TuiTheme, load_tui_theme
from vibesys.api import ConfigurationError
from vibesys.api.request import generate_experiment_name, repository_name_from_experiment
from vs_github.api import GitHubCLI, GitHubCLIError
from vs_project.api import Project

_WEB_PORT_MAX = 65_535
_DETACHED_START_TIMEOUT_SECONDS = 10.0
_DETACHED_STOP_TIMEOUT_SECONDS = 2.0
GATEWAY_STOP_TIMEOUT_SECONDS = 10.0
"""How long `stop_detached_gateway` waits for a gateway to release its files."""
_DETACHED_POLL_SECONDS = 0.05
_DETACHED_LOG_TAIL_BYTES = 4_096

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable
    from typing import BinaryIO

    from vibesys.api import Config


def _control_socket_from_argv(argv: list[str]) -> Path | None:
    """Read the transport bootstrap flag without parsing run configuration."""
    value = cli.option_from_argv(argv, "--control-socket")
    return Path(value) if value else None


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


def _web_assets_from_argv(argv: list[str]) -> Path | None:
    value = cli._option_from_argv(argv, "--web-assets")  # noqa: SLF001  # lint-waiver: LW-101004 [SLF001]; reuse the CLI's private option scanner before full argument parsing
    if value is not None:
        return Path(value).expanduser().resolve()
    source_root = Path(__file__).resolve().parents[2]
    candidate = source_root / "clients" / "web" / "dist"
    return candidate if candidate.is_dir() else None


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


def _read_only_log_from_argv(argv: list[str]) -> Path | None:
    value = cli._option_from_argv(argv, "--web-reopen")  # noqa: SLF001  # lint-waiver: LW-101043 [SLF001]; reuse the CLI's private option scanner for the launcher-only flag
    if value is None:
        return None
    path = Path(value).expanduser().resolve()
    return path.parent if path.name == "run-events.jsonl" else path


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
            "--theme",
            "--web-port",
            "--web-assets",
            "--web-origin",
            "--web-instance",
            "--web-reopen",
        }:
            skip_next = True
            continue
        if argument in {"--web", "--detach"}:
            continue
        if argument.startswith(
            (
                "--control-socket=",
                "--theme=",
                "--web-port=",
                "--web-assets=",
                "--web-origin=",
                "--web-instance=",
                "--web-reopen=",
            )
        ):
            continue
        arguments.append(argument)
    return arguments


def _suggest_repository_owner(config: Config) -> str | None:
    """Return a setup-form owner suggestion without requiring GitHub access."""
    repository = config.repository
    if repository.owner is not None:
        return str(repository.owner)
    try:
        return GitHubCLI().current_user()
    except GitHubCLIError:
        return None


def _resolve_tui_defaults(  # noqa: PLR0913  # lint-waiver: LW-011105 [PLR0913]; This private resolver maps the parser's six independent setup flags to derived defaults; a Namespace loses field types and a new input DTO would duplicate the parser.
    *,
    config_path: Path | None = None,
    input_path: Path | None = None,
    runs_dir: Path | None = None,
    experiment_name: str | None = None,
    theme: TuiTheme | None = None,
    directory_only: bool = False,
) -> InteractiveSetupDefaults:
    """Resolve launcher-facing defaults from local configuration."""
    config = cli.load_config_or_default(config_path)
    launch_config_path = config_path
    if launch_config_path is None:
        directory_config = Path.cwd() / "agent.toml"
        launch_config_path = directory_config if directory_config.is_file() else None
    resolved_input = input_path.expanduser().resolve() if input_path is not None else None
    resolved_runs_dir = (runs_dir or Path.cwd() / "exp_env").expanduser().resolve()
    resolved_name = experiment_name or generate_experiment_name(resolved_input)
    return InteractiveSetupDefaults(
        runs_dir=str(resolved_runs_dir),
        input_path=str(resolved_input) if resolved_input is not None else "",
        experiment_name=resolved_name,
        repository_owner=None if directory_only else _suggest_repository_owner(config),
        repository_name=repository_name_from_experiment(resolved_name),
        visibility=config.repository.visibility,
        theme=theme or load_tui_theme(launch_config_path),
    )


def _tui_defaults_from_argv(argv: list[str]) -> Callable[[], InteractiveSetupDefaults]:
    """Build the lazy defaults provider exposed over the control socket."""
    config = cli.option_from_argv(argv, "--config")
    theme = cli.option_from_argv(argv, "--theme")

    def provide() -> InteractiveSetupDefaults:
        return _resolve_tui_defaults(
            config_path=Path(config) if config is not None else None,
            theme=TuiTheme(theme) if theme is not None else None,
            directory_only=True,
        )

    return provide


def _build_tui_defaults_parser() -> argparse.ArgumentParser:
    parser = cli.RunArgumentParser(
        prog="vibesys tui-defaults",
        description="Resolve configuration defaults for a TUI launcher.",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--runs-dir", type=cli.parse_runs_dir, default=None)
    parser.add_argument("--exp-name", default=None)
    parser.add_argument("--theme", type=TuiTheme, choices=list(TuiTheme), default=None)
    parser.add_argument("--directory-only", action="store_true")
    return parser


def _run_tui_defaults(argv: list[str]) -> None:
    args = _build_tui_defaults_parser().parse_args(argv)
    try:
        defaults = _resolve_tui_defaults(
            config_path=args.config,
            input_path=args.input,
            runs_dir=args.runs_dir,
            experiment_name=args.exp_name,
            theme=args.theme,
            directory_only=args.directory_only,
        )
    except (ValueError, FileNotFoundError) as exc:
        cli.configuration_error(
            str(exc),
            code="config_load_failed",
            stage="config_loading",
        )
    sys.stdout.write(defaults.model_dump_json() + "\n")


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
    and `discover`.
    """

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
        return WebInstanceRecord.discover(instance_path, cleanup_stale=False)

    def read_record(self, instance_path: Path) -> WebInstanceRecord | None:
        return WebInstanceRecord.read(instance_path)

    def observe(self, instance_path: Path) -> WebInstanceHold:
        return WebInstanceHold.observe(instance_path)

    def terminate(self, pid: int) -> None:
        os.kill(pid, signal.SIGTERM)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


_DETACHED_EFFECTS = _DetachedGatewayEffects()


def _spawn_detached(
    arguments: list[str],
    instance_path: Path,
    effects: _DetachedGatewayEffects = _DETACHED_EFFECTS,
) -> None:
    """Start the long-lived child and wait for its capability record."""
    environment = {**os.environ, "VIBESYS_DETACHED_CHILD": "1"}
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
        process = effects.spawn(
            [sys.executable, "-m", "entrypoints.server", *arguments],
            environment,
            output,
        )
        deadline = effects.monotonic() + _DETACHED_START_TIMEOUT_SECONDS
        while effects.monotonic() < deadline:
            record = effects.discover(instance_path)
            if record is not None:
                print(f"VibeSys web UI: {record.url}", flush=True)  # noqa: T201  # lint-waiver: LW-101036 [T201]; expose the detached capability URL to the launcher user
                webbrowser.open(record.url, new=2)
                return
            status = process.poll()
            if status is not None:
                raise RuntimeError(
                    _detached_failure(
                        f"Detached VibeSys web gateway exited with status {status}",
                        log_path,
                        output,
                    )
                )
            effects.sleep(_DETACHED_POLL_SECONDS)
        _stop_detached(process)
        raise RuntimeError(
            _detached_failure(
                "Detached VibeSys web gateway did not become ready within 10 seconds",
                log_path,
                output,
            )
        )


def _instance_in_use(instance_path: Path, hold: WebInstanceHold) -> str:
    """Explain a refused launch, naming the processes in the way, if it can.

    The blocker is whatever holds the startup log, which need not be a VibeSys
    gateway and need not be stoppable with `vibesys web stop`: a descendant
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
        f"gateway's output.{users} Stop the gateway with `vibesys web stop --instance "
        f"{instance_path}`, or end those processes yourself if no gateway owns them."
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
    effects: WebGatewayStopEffects | None = None,
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
    effects = _DETACHED_EFFECTS if effects is None else effects
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


def _detached_failure(summary: str, log_path: Path, output: BinaryIO) -> str:
    output.flush()
    length = output.seek(0, os.SEEK_END)
    output.seek(max(0, length - _DETACHED_LOG_TAIL_BYTES))
    tail = output.read().decode("utf-8", errors="replace").strip()
    message = f"{summary}. Startup log: {log_path}"
    return f"{message}\n{tail}" if tail else message


def _discover_web_instance(path: Path) -> WebInstanceRecord | None:
    """Wait through the bind-to-record race before deciding to launch again."""
    deadline = time.monotonic() + 2
    while True:
        record = WebInstanceRecord.discover(path, cleanup_stale=False)
        if record is not None:
            return record
        if not WebInstanceClaim.is_held(path):
            return WebInstanceRecord.discover(path)
        if time.monotonic() >= deadline:
            return WebInstanceRecord.discover(path)
        time.sleep(0.05)


def main(argv: list[str] | None = None) -> None:
    """Run the frontend server and headless engine in one process.

    Every configuration diagnostic raised below this frame is rendered to
    stderr and exits with the diagnostic's code, the way
    `entrypoints.headless.main` does. One handler here rather than one per
    raising site is what makes that total: argument parsing and `RunRequest`
    building both run before any transport binds, so a diagnostic they raise
    has no structured channel to reach a frontend on, and stderr is inherited
    by the TUI launcher and redirected onto the detached startup log.
    """
    try:
        _serve(sys.argv[1:] if argv is None else argv)
    except ConfigurationError as exc:
        cli.render_configuration_error(exc)


def _serve(arguments: list[str]) -> None:  # noqa: C901, PLR0912, PLR0915  # lint-waiver: LW-101031 [C901, PLR0912, PLR0915]; the entrypoint owns ordered setup, parsing, execution, and cleanup branches
    """Serve one invocation, leaving configuration diagnostics to `main`."""
    if arguments and arguments[0] == "tui-defaults":
        _run_tui_defaults(arguments[1:])
        return

    web = _web_requested(arguments)
    detach = _detach_requested(arguments)
    if detach and not web:
        cli.configuration_error(
            "--detach requires --web",
            code="invalid_arguments",
            stage="argument_parsing",
        )
    instance_path = _web_instance_from_argv(arguments) if web else None
    if web and os.environ.get("VIBESYS_DETACHED_CHILD") != "1":
        if instance_path is None:  # pragma: no cover - web always supplies a path.
            raise RuntimeError("Web instance path was not resolved")  # noqa: TRY003  # lint-waiver: LW-101038 [TRY003]; guard an impossible parser/launcher invariant
        existing = _discover_web_instance(instance_path)
        if existing is not None:
            print(f"VibeSys web UI: {existing.url}", flush=True)  # noqa: T201  # lint-waiver: LW-101039 [T201]; expose the reused capability URL to the launcher user
            webbrowser.open(existing.url, new=2)
            return
        if detach:
            try:
                _spawn_detached(arguments, instance_path)
            except RuntimeError as exc:
                # A refused or failed detached launch is an operator-facing
                # outcome, not a defect, so it reports a message rather than a
                # traceback. `_spawn_detached` has already released the log.
                sys.stderr.write(f"{exc}\n")
                raise SystemExit(1) from None
            return
    control_socket = _control_socket_from_argv(arguments)
    temp_socket_dir: tempfile.TemporaryDirectory[str] | None = None
    if control_socket is None and web:
        temp_socket_dir = tempfile.TemporaryDirectory(prefix="vibesys-web-")
        control_socket = Path(temp_socket_dir.name) / "control.sock"
    if control_socket is None:
        _missing_control_socket()
    try:
        web_port = _web_port_from_argv(arguments)
        web_assets = _web_assets_from_argv(arguments)
        web_origins = _web_origins_from_argv(arguments)
        read_only_log = _read_only_log_from_argv(arguments)
    except ValueError as exc:
        cli.configuration_error(
            str(exc),
            code="invalid_arguments",
            stage="argument_parsing",
        )
    server_runtime = import_module("server.runtime").ServerRuntime
    try:
        if web:
            runtime = server_runtime(
                socket_path=control_socket,
                tui_defaults=_tui_defaults_from_argv(arguments),
                web=True,
                web_port=web_port,
                web_assets=web_assets,
                web_origins=web_origins,
                instance_path=instance_path,
                detach=detach,
                read_only_log=read_only_log,
            )
        else:
            runtime = server_runtime(
                socket_path=control_socket,
                tui_defaults=_tui_defaults_from_argv(arguments),
            )
        if read_only_log is not None:
            result = runtime.run(lambda: None)
        else:
            invocation = cli.parse_cli_invocation(_headless_argv(arguments))
            request = cli.build_run_request(invocation)
            result = runtime.run(lambda: runtime.drive(request))
    finally:
        if temp_socket_dir is not None:
            temp_socket_dir.cleanup()
    # `result` is `None` when `ServerRuntime.run` absorbed an operator stop
    # (`RunStopped`) as a clean backend exit; only a completed run's
    # `RunResult.succeeded` decides the process exit code.
    if result is not None and not result.succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
