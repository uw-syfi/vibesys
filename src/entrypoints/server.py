"""Serving entrypoint for VibeSys frontend clients."""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from entrypoints import cli
from server.settings import InteractiveSetupDefaults, TuiTheme, load_tui_theme
from vibesys.api import ConfigurationError
from vibesys.api.request import generate_experiment_name, repository_name_from_experiment
from vs_github.api import GitHubCLI, GitHubCLIError

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from vibesys.api import Config


def _control_socket_from_argv(argv: list[str]) -> Path | None:
    """Read the transport bootstrap flag without parsing run configuration."""
    value = cli.option_from_argv(argv, "--control-socket")
    return Path(value) if value else None


def _headless_argv(argv: list[str]) -> list[str]:
    """Remove server-only options before dispatching to the core CLI."""
    arguments: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token in {"--control-socket", "--theme"}:
            skip_next = True
            continue
        if token.startswith(("--control-socket=", "--theme=")):
            continue
        arguments.append(token)
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


def _resolve_tui_defaults(  # noqa: PLR0913  # lint-waiver: LW-011105 [PLR0913]; This private resolver maps the parser's seven independent setup flags to derived defaults; a Namespace loses field types and a new input DTO would duplicate the parser.
    *,
    config_path: Path | None = None,
    stub_agent: bool = False,
    input_path: Path | None = None,
    runs_dir: Path | None = None,
    experiment_name: str | None = None,
    theme: TuiTheme | None = None,
    directory_only: bool = False,
) -> InteractiveSetupDefaults:
    """Resolve launcher-facing defaults from local configuration."""
    config = cli.load_config_or_stub_default(
        config_path,
        stub_agent=stub_agent,
    )
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
    stub_agent = "--stub-agent" in argv

    def provide() -> InteractiveSetupDefaults:
        return _resolve_tui_defaults(
            config_path=Path(config) if config is not None else None,
            stub_agent=stub_agent,
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
    parser.add_argument("--stub-agent", action="store_true")
    parser.add_argument("--directory-only", action="store_true")
    return parser


def _run_tui_defaults(argv: list[str]) -> None:
    args = _build_tui_defaults_parser().parse_args(argv)
    try:
        defaults = _resolve_tui_defaults(
            config_path=args.config,
            stub_agent=args.stub_agent,
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


def main(argv: list[str] | None = None) -> None:
    """Run the frontend server and headless engine in one process."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "tui-defaults":
        try:
            _run_tui_defaults(arguments[1:])
        except ConfigurationError as exc:
            cli.render_configuration_error(exc)
        return

    control_socket = _control_socket_from_argv(arguments)
    if control_socket is None:
        try:
            _missing_control_socket()
        except ConfigurationError as exc:
            cli.render_configuration_error(exc)
    server_runtime = import_module("server.runtime").ServerRuntime

    runtime = server_runtime(
        socket_path=control_socket,
        tui_defaults=_tui_defaults_from_argv(arguments),
    )
    try:
        invocation = cli.parse_cli_invocation(_headless_argv(arguments))
        request = cli.build_run_request(invocation)
        result = runtime.run(lambda: runtime.drive(request))
    except ConfigurationError as exc:
        raise SystemExit(exc.diagnostic.exit_code) from None
    # `result` is `None` when `ServerRuntime.run` absorbed an operator stop
    # (`RunStopped`) as a clean backend exit; only a completed run's
    # `RunResult.succeeded` decides the process exit code.
    if result is not None and not result.succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
