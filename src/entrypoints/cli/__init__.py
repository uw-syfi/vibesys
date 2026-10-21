"""Shared VibeSys CLI: argument parsing and ``RunRequest`` building.

Both the headless mode entry (``entrypoints.headless``) and the frontend
server entry (``entrypoints.server``) build their run configuration through
this package. Neither mode-entry module reaches into the other's internals;
this package is the shared surface between them.

The loop is picked by ``--outer-loop {agent, profile-guided, plain, evolve}``:

  "agent" / "profile-guided": orchestrated hypothesis loops. The designer's
             roadmap is kept in the run's state; the profiler's findings travel in its reply.
  "plain": issue-driven outer loop on the core lifecycle. Its issue board (a local
             issues.json, or GitHub issues) is filled by the performance evaluator
             and the judge, and the implementer works one issue at a time.
  "evolve": population-based evolutionary search.

The module is split into submodules by concern (``args`` for argparse wiring,
``config``/``inputs``/``remote``/``resume`` for the per-command preamble,
``loops`` for the request builder and runner). This package's
top level owns dispatch and re-exports the names those submodules and the
mode entries share.
"""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from entrypoints.cli.args import (
    _add_common_args,
    _build_agent_parser,
    _build_evolve_parser,
    _build_plain_parser,
    _extract_flag,
    _extract_loop_selection,
    _parse_cli_objective,
    _parse_runs_dir,
)
from entrypoints.cli.compare import run as run_compare
from entrypoints.cli.config import (
    _load_config_or_default,
    _prepare_experiment_repository,
    load_config_and_skills,
)
from entrypoints.cli.constants import _OUTER_LOOPS
from entrypoints.cli.environment import run_environment_spec_from_args
from entrypoints.cli.errors import _configuration_error, _RunArgumentParser
from entrypoints.cli.inputs import (
    _apply_bundle_profiler_default,
    _run_validate,
    _validate_agent,
    _validate_target_inputs,
)
from entrypoints.cli.loops import (
    _build_run_request,
    _load_metric_space_toml,
    _normalize_runs_dir,
    _resolve_openevolve_options,
    _run_request,
    _validate_evolve,
    _validate_plain,
    validated_run_request,
)
from entrypoints.cli.preflight import run_preflight
from entrypoints.cli.resume import _resolve_resume_args, reader_loop_kind
from entrypoints.control import control_main, time_ordered_id
from vibesys.api import ConfigurationError, RunRequest, boot_trace
from vibesys.api.request import (
    generate_experiment_name,
    load_objective,
    with_operator_constraints,
)
from vs_project.api import generate_run_id
from vs_sim.api import MonotonicClock, SystemClock, follow_launcher, utc_datetime

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from vibesys.api import Runs

# Aliases the tests reach for by name; the submodules use the imported names.
_load_objective = load_objective
_with_operator_constraints = with_operator_constraints

__all__ = [
    "_OUTER_LOOPS",
    "CliInvocation",
    "RunArgumentParser",
    "_RunArgumentParser",
    "_add_common_args",
    "_apply_bundle_profiler_default",
    "_build_agent_parser",
    "_build_evolve_parser",
    "_build_plain_parser",
    "_configuration_error",
    "_extract_flag",
    "_extract_loop_selection",
    "_load_config_or_default",
    "_load_metric_space_toml",
    "_load_objective",
    "_option_from_argv",
    "_parse_cli_objective",
    "_parse_runs_dir",
    "_prepare_experiment_repository",
    "_render_configuration_error",
    "_resolve_openevolve_options",
    "_run_validate",
    "_validate_agent",
    "_validate_target_inputs",
    "_with_operator_constraints",
    "build_reader_request",
    "build_run_request",
    "configuration_error",
    "dispatch",
    "load_config_and_skills",
    "load_config_or_default",
    "option_from_argv",
    "parse_cli_invocation",
    "parse_runs_dir",
    "render_configuration_error",
    "run_environment_spec_from_args",
    "start_process",
]


@dataclass(frozen=True)
class CliInvocation:
    """One parsed, validated CLI invocation: its loop kind and namespace."""

    loop_kind: str
    args: argparse.Namespace


@dataclass(frozen=True)
class _LoopCommand:
    """Parser and validation for one CLI outer-loop selection."""

    build_parser: Callable[[], _RunArgumentParser]
    validate: Callable[[argparse.Namespace], None]


_LOOP_COMMANDS: dict[str, _LoopCommand] = {
    "agent": _LoopCommand(_build_agent_parser, _validate_agent),
    "profile-guided": _LoopCommand(_build_agent_parser, _validate_agent),
    "dynamic": _LoopCommand(_build_agent_parser, _validate_agent),
    "plain": _LoopCommand(_build_plain_parser, _validate_plain),
    "evolve": _LoopCommand(_build_evolve_parser, _validate_evolve),
}


def build_run_request(invocation: CliInvocation) -> RunRequest:
    """Build the ``RunRequest`` for one already-parsed CLI invocation.

    Runs the shared config, skills, repository, environment, and descriptor
    preamble without starting the run.
    """
    return _build_run_request(invocation.args)


def build_reader_request(project_root: Path, run_id: str) -> RunRequest:
    """Restore saved settings without switching branches, resuming or building images."""
    invocation = parse_cli_invocation(
        [
            "--outer-loop",
            reader_loop_kind(project_root, run_id),
            "--project",
            str(project_root),
            "--resume",
            run_id,
        ],
        read_only=True,
    )
    return validated_run_request(invocation.args, read_only=True)[0]


def _explicit_cli_dests(
    parser: argparse.ArgumentParser,
    argv: list[str],
) -> frozenset[str]:
    """Return parser destinations whose option strings occur in *argv*."""
    destinations = {
        option: action.dest
        for action in parser._actions  # noqa: SLF001  # lint-waiver: LW-009023 [SLF001]; argparse has no public API for enumerating configured option actions.
        for option in action.option_strings
    }
    return frozenset(
        destinations[option]
        for token in argv
        if token.startswith("-") and (option := token.partition("=")[0]) in destinations
    )


def parse_cli_invocation(argv: list[str], *, read_only: bool = False) -> CliInvocation:
    """Parse and validate one invocation without printing or exiting."""
    loop_kind, remaining = _extract_loop_selection(argv)
    command = _LOOP_COMMANDS[loop_kind]
    parser = command.build_parser()
    parser.prog = f"vibesys --outer-loop {loop_kind}"
    args = parser.parse_run_arguments(remaining)
    args.outer_loop = loop_kind
    args.explicit_cli_dests = _explicit_cli_dests(parser, remaining)
    _normalize_runs_dir(args)
    _resolve_resume_args(args, loop_kind=loop_kind, read_only=read_only)
    command.validate(args)
    _allocate_run_id(args)
    return CliInvocation(loop_kind=loop_kind, args=args)


def _allocate_run_id(args: argparse.Namespace) -> None:
    """Fix the run's durable id once, before anything starts or detaches.

    A resume has resolved its run already; a fresh run keeps a launcher's
    ``--run-id`` or gets a new one, so every consumer reads ``args.run_id``.
    """
    if args.run_id is not None:
        return
    if args.exp_name is None:
        args.exp_name = generate_experiment_name(
            args.input_bundle.root, now=utc_datetime(SystemClock())
        )
    args.run_id = generate_run_id(args.exp_name, now=utc_datetime(SystemClock()))


def start_process() -> None:
    """Begin a run process: follow its launcher, then time its boot stages on the real clocks.

    Started by the ``vibesys`` launcher, the process hangs up when the launcher exits, by
    any means (``follow_launcher``).
    """
    follow_launcher(os.environ)
    boot_trace.install(boot_trace.BootTrace(wall=SystemClock(), elapsed=MonotonicClock()))


def dispatch(argv: list[str], runs: Runs | None = None) -> None:
    """Parse and run one headless VibeSys invocation, on ``runs`` or the default launcher."""
    if argv and argv[0] == "relaunch":
        argv = argv[1:]
        if _option_from_argv(argv, "--resume") is None:
            _configuration_error(
                "relaunch requires --resume RUN",
                code="relaunch_requires_run",
                stage="resume_resolution",
            )
    if argv and argv[0] == "validate":
        _run_validate(argv[1:])
        return
    if argv and argv[0] == "compare":
        raise SystemExit(run_compare(argv[1:]))
    if argv and argv[0] == "control":
        raise SystemExit(
            control_main(
                argv[1:],
                cwd=Path.cwd(),
                new_control_id=lambda: time_ordered_id(SystemClock().now(), secrets.token_hex(4)),
                out=sys.stdout,
                err=sys.stderr,
            )
        )
    if argv and argv[0] == "preflight":
        run_preflight(argv[1:], lambda arguments: parse_cli_invocation(arguments).args)
        return
    with boot_trace.span("dispatch"), boot_trace.span("parse_cli_invocation"):
        invocation = parse_cli_invocation(argv)
    _run_request(invocation.args, runs)


def _option_from_argv(argv: list[str], option: str) -> str | None:
    """Read one option's value without parsing the full run configuration."""
    prefix = f"{option}="
    for index, token in enumerate(argv):
        if token.startswith(prefix):
            return token[len(prefix) :]
        if token == option and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _render_configuration_error(error: ConfigurationError) -> NoReturn:
    """Print a configuration error to stderr and exit with its code."""
    diagnostic = error.diagnostic
    sys.stderr.write(f"vibesys: {diagnostic.message}\n")
    if diagnostic.usage:
        sys.stderr.write(diagnostic.usage + "\n")
    raise SystemExit(diagnostic.exit_code)


# Public bootstrap helpers shared with the serving entrypoint.
RunArgumentParser = _RunArgumentParser
configuration_error = _configuration_error
load_config_or_default = _load_config_or_default
option_from_argv = _option_from_argv
parse_runs_dir = _parse_runs_dir
render_configuration_error = _render_configuration_error
