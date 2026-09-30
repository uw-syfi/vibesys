"""Serving entrypoint for VibeSys frontend clients."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import webbrowser
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol

from entrypoints import cli
from server.runtime import WebInstanceClaim, WebInstanceRecord, browser_origin
from server.settings import InteractiveSetupDefaults, TuiTheme, load_tui_theme
from vibesys.api import ConfigurationError
from vibesys.api.request import generate_experiment_name, repository_name_from_experiment
from vs_github.api import GitHubCLI, GitHubCLIError
from vs_project.api import Project

_WEB_PORT_MAX = 65_535
_DETACHED_START_TIMEOUT_SECONDS = 10.0
_DETACHED_STOP_TIMEOUT_SECONDS = 2.0
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


class _DetachedLaunchEffects:
    """Process, discovery, and clock effects for detached gateway startup."""

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

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


_DETACHED_EFFECTS = _DetachedLaunchEffects()


def _spawn_detached(
    arguments: list[str],
    instance_path: Path,
    effects: _DetachedLaunchEffects = _DETACHED_EFFECTS,
) -> None:
    """Start the long-lived child and wait for its capability record."""
    environment = {**os.environ, "VIBESYS_DETACHED_CHILD": "1"}
    instance_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = instance_path.with_name(f"{instance_path.name}.log")
    descriptor = os.open(log_path, os.O_CREAT | os.O_TRUNC | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w+b") as output:
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
            effects.sleep(0.05)
        _stop_detached(process)
        raise RuntimeError(
            _detached_failure(
                "Detached VibeSys web gateway did not become ready within 10 seconds",
                log_path,
                output,
            )
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


def main(argv: list[str] | None = None) -> None:  # noqa: C901, PLR0912, PLR0915  # lint-waiver: LW-101031 [C901, PLR0912, PLR0915]; the entrypoint owns ordered setup, parsing, execution, and cleanup branches
    """Run the frontend server and headless engine in one process."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "tui-defaults":
        try:
            _run_tui_defaults(arguments[1:])
        except ConfigurationError as exc:
            cli.render_configuration_error(exc)
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
            _spawn_detached(arguments, instance_path)
            return
    control_socket = _control_socket_from_argv(arguments)
    temp_socket_dir: tempfile.TemporaryDirectory[str] | None = None
    if control_socket is None and web:
        temp_socket_dir = tempfile.TemporaryDirectory(prefix="vibesys-web-")
        control_socket = Path(temp_socket_dir.name) / "control.sock"
    if control_socket is None:
        try:
            _missing_control_socket()
        except ConfigurationError as exc:
            cli.render_configuration_error(exc)
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
        try:
            if read_only_log is not None:
                result = runtime.run(lambda: None)
            else:
                invocation = cli.parse_cli_invocation(_headless_argv(arguments))
                request = cli.build_run_request(invocation)
                result = runtime.run(lambda: runtime.drive(request))
        except ConfigurationError as exc:
            raise SystemExit(exc.diagnostic.exit_code) from None
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
