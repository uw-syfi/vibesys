"""Serving entrypoint for VibeSys frontend clients."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import webbrowser
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import urlsplit

from entrypoints import cli
from server.runtime import WebInstanceClaim, WebInstanceRecord
from server.settings import InteractiveSetupDefaults, TuiTheme, load_tui_theme
from vibesys.api import ConfigurationError, open_run_store
from vibesys.api.request import generate_experiment_name, repository_name_from_experiment
from vs_github.api import GitHubCLI, GitHubCLIError
from vs_project.api import Project, ProjectError

_WEB_PORT_MAX = 65_535

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from vibesys.api import Config, RunRecord


def _control_socket_from_argv(argv: list[str]) -> Path | None:
    """Read the transport bootstrap flag without parsing run configuration."""
    value = cli.option_from_argv(argv, "--control-socket")
    return Path(value) if value else None


_WEB_FLAGS = frozenset({"--web", "--web-reopen", "--web-reopen-run"})


def _web_requested(argv: list[str]) -> bool:
    return any(argument.partition("=")[0] in _WEB_FLAGS for argument in argv)


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
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(  # noqa: TRY003  # lint-waiver: LW-101069 [TRY003]; reject browser origins that cannot satisfy the exact WebSocket origin policy
                "--web-origin must be an http:// or https:// origin without a path"
            )
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


def _web_instance_from_argv(argv: list[str]) -> Path:
    value = cli._option_from_argv(argv, "--web-instance")  # noqa: SLF001  # lint-waiver: LW-101042 [SLF001]; reuse the CLI's private option scanner for the launcher-only flag
    if value is not None:
        return Path(value).expanduser().resolve()
    run_id = cli.option_from_argv(argv, "--web-reopen-run") or None
    log_dir = _read_only_log_from_argv(argv)
    if run_id is None and log_dir is not None:
        run_id = "log-" + hashlib.sha256(str(log_dir).encode()).hexdigest()[:12]
    name = "web-gateway.json" if run_id is None else f"web-gateway-{run_id}.json"
    return (Project.open(_project_root_from_argv(argv)).configuration_path() / name).resolve()


def _read_only_log_from_argv(argv: list[str]) -> Path | None:
    value = cli._option_from_argv(argv, "--web-reopen")  # noqa: SLF001  # lint-waiver: LW-101043 [SLF001]; reuse the CLI's private option scanner for the launcher-only flag
    if value is None:
        return None
    path = Path(value).expanduser().resolve()
    return path.parent if path.name == "run-events.jsonl" else path


def _project_root_from_argv(argv: list[str]) -> Path:
    value = cli.option_from_argv(argv, "--project")
    return Path(value).expanduser().resolve() if value else Path.cwd()


def _run_id_from_argv(argv: list[str]) -> str | None:
    """Return the ``--web-reopen-run`` value, rejecting a present-but-empty one.

    ``cli.option_from_argv`` returns ``None`` both when the flag is absent and
    when it is the last, valueless argument, and returns ``""`` for
    ``--web-reopen-run=``; all three are distinct: only a genuinely absent
    flag should mean "no reopen requested".
    """
    value = cli.option_from_argv(argv, "--web-reopen-run")
    if value:
        return value
    if any(
        argument == "--web-reopen-run" or argument.startswith("--web-reopen-run=")
        for argument in argv
    ):
        cli.configuration_error(
            "--web-reopen-run requires a non-empty run ID",
            code="invalid_arguments",
            stage="argument_parsing",
        )
    return None


def _reopen_from_argv(argv: list[str]) -> tuple[Path | None, RunRecord | None]:
    """Resolve the read-only journal and, with ``--web-reopen-run``, its run record.

    Only the journal's first event is checked here, so a wrong journal fails
    before launch; ``RunController.attach_read_only`` checks every event.
    """
    log_dir = _read_only_log_from_argv(argv)
    record = None
    run_id = _run_id_from_argv(argv)
    if run_id is not None:
        project = Project.open(_project_root_from_argv(argv))
        record = open_run_store(project).get_record(run_id)
        log_dir = log_dir or project.state.log_directory_path(run_id)
    if log_dir is None:
        return None, None
    events = log_dir / "run-events.jsonl"
    if not events.is_file():
        cli.configuration_error(
            f"No event journal to reopen at {log_dir}",
            code="invalid_arguments",
            stage="argument_parsing",
        )
    if record is not None:
        with events.open(encoding="utf-8") as stream:
            journal_run = json.loads(stream.readline() or "{}").get("run_id")
        if journal_run != record.run_id:
            cli.configuration_error(
                f"Journal {log_dir} belongs to run {journal_run}, not {record.run_id}",
                code="invalid_arguments",
                stage="argument_parsing",
            )
    return log_dir, record


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


def _spawn_detached(arguments: list[str], instance_path: Path) -> None:
    """Start the long-lived child and wait for its capability record."""
    environment = {**os.environ, "VIBESYS_DETACHED_CHILD": "1"}
    subprocess.Popen(  # noqa: S603  # lint-waiver: LW-101035 [S603]; launch the detached child with a fixed interpreter/module command
        [sys.executable, "-m", "entrypoints.server", *arguments],
        cwd=Path.cwd(),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        record = WebInstanceRecord.discover(instance_path, cleanup_stale=False)
        if record is not None:
            print(f"VibeSys web UI: {record.url}", flush=True)  # noqa: T201  # lint-waiver: LW-101036 [T201]; expose the detached capability URL to the launcher user
            webbrowser.open(record.url, new=2)
            return
        time.sleep(0.05)
    raise RuntimeError("Detached VibeSys web gateway did not become ready")  # noqa: TRY003  # lint-waiver: LW-101037 [TRY003]; report a bounded child-startup failure to the launcher


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
    try:
        read_only_log, read_only_record = _reopen_from_argv(arguments)
        instance_path = _web_instance_from_argv(arguments) if web else None
    except (ValueError, ProjectError) as exc:
        cli.configuration_error(
            str(exc),
            code="invalid_arguments",
            stage="argument_parsing",
        )
    if web and os.environ.get("VIBESYS_DETACHED_CHILD") != "1":
        if instance_path is None:  # pragma: no cover - web always supplies a path.
            raise RuntimeError("Web instance path was not resolved")  # noqa: TRY003  # lint-waiver: LW-101038 [TRY003]; guard an impossible parser/launcher invariant
        existing = _discover_web_instance(instance_path)
        if existing is not None:
            requested = (
                "live" if read_only_log is None else "reopen",
                read_only_record.run_id if read_only_record is not None else None,
            )
            if (existing.mode, existing.run_id) != requested:
                run_suffix = "" if existing.run_id is None else f" for run {existing.run_id}"
                cli.configuration_error(
                    f"{instance_path} is held by a {existing.mode} gateway{run_suffix}",
                    code="invalid_arguments",
                    stage="argument_parsing",
                )
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
                read_only_record=read_only_record,
                project_root=_project_root_from_argv(arguments),
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
