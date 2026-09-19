"""Serving entrypoint for VibeSys frontend clients."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from entrypoints import headless
from server.settings import TuiTheme, load_tui_theme
from server.wire import codec, enums
from server.wire.v2 import responses_pb2
from vibesys.errors import ConfigurationError
from vibesys.repository import (
    generate_experiment_name,
    repository_name_from_experiment,
)
from vs_github import GitHubCLI, GitHubCLIError

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from vibesys.config import Config


def _control_socket_from_argv(argv: list[str]) -> Path | None:
    """Read the transport bootstrap flag without parsing run configuration."""
    value = headless._option_from_argv(argv, "--control-socket")  # noqa: SLF001
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


def _resolve_tui_defaults(  # noqa: PLR0913  # tracked: #288
    *,
    config_path: Path | None = None,
    stub_agent: bool = False,
    input_path: Path | None = None,
    runs_dir: Path | None = None,
    experiment_name: str | None = None,
    theme: TuiTheme | None = None,
    directory_only: bool = False,
) -> responses_pb2.TuiDefaults:
    """Resolve launcher-facing defaults from local configuration."""
    config = headless._load_config_or_stub_default(  # noqa: SLF001
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
    return responses_pb2.TuiDefaults(
        runs_dir=str(resolved_runs_dir),
        input_path=str(resolved_input) if resolved_input is not None else "",
        experiment_name=resolved_name,
        repository_owner=None if directory_only else _suggest_repository_owner(config),
        repository_name=repository_name_from_experiment(resolved_name),
        visibility=enums.number(responses_pb2.RepositoryVisibility, config.repository.visibility),
        theme=enums.number(responses_pb2.TuiTheme, theme or load_tui_theme(launch_config_path)),
    )


def _tui_defaults_from_argv(argv: list[str]) -> Callable[[], responses_pb2.TuiDefaults]:
    """Build the lazy defaults provider exposed over the control socket."""
    config = headless._option_from_argv(argv, "--config")  # noqa: SLF001
    theme = headless._option_from_argv(argv, "--theme")  # noqa: SLF001
    stub_agent = "--stub-agent" in argv

    def provide() -> responses_pb2.TuiDefaults:
        return _resolve_tui_defaults(
            config_path=Path(config) if config is not None else None,
            stub_agent=stub_agent,
            theme=TuiTheme(theme) if theme is not None else None,
            directory_only=True,
        )

    return provide


def _build_tui_defaults_parser() -> argparse.ArgumentParser:
    parser = headless._RunArgumentParser(  # noqa: SLF001
        prog="vibesys tui-defaults",
        description="Resolve configuration defaults for a TUI launcher.",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--runs-dir", type=headless._parse_runs_dir, default=None)  # noqa: SLF001
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
        headless._configuration_error(  # noqa: SLF001
            str(exc),
            code="config_load_failed",
            stage="config_loading",
        )
    print(codec.dumps(defaults))  # noqa: T201  # tracked: #288


def _missing_control_socket() -> NoReturn:
    headless._configuration_error(  # noqa: SLF001
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
            headless._render_configuration_error(exc)  # noqa: SLF001
        return

    control_socket = _control_socket_from_argv(arguments)
    if control_socket is None:
        try:
            _missing_control_socket()
        except ConfigurationError as exc:
            headless._render_configuration_error(exc)  # noqa: SLF001
    from server.runtime import ServerRuntime  # noqa: PLC0415  # tracked: #288

    runtime = ServerRuntime(
        socket_path=control_socket,
        tui_defaults=_tui_defaults_from_argv(arguments),
    )
    try:
        runtime.run(
            lambda: headless.dispatch(
                _headless_argv(arguments),
                integration=runtime.integration,
            )
        )
    except ConfigurationError as exc:
        raise SystemExit(exc.diagnostic.exit_code) from None


if __name__ == "__main__":
    main()
