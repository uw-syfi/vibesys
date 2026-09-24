"""Shared VibeSys CLI: argument parsing and ``RunRequest`` building.

Both the headless mode entry (``entrypoints.headless``) and the frontend
server entry (``entrypoints.server``) build their run configuration through
this package. Neither mode-entry module reaches into the other's internals;
this package is the shared surface between them.

The loop is picked by ``--outer-loop {agent, profile-guided, plain, evolve}``:

  "agent" / "profile-guided": orchestrated hypothesis loops.
             Its issue board lives in the workspace as roadmap.md +
             progress.md, owned by the orchestrator.
  "plain": deterministic outer loop. Its issue board is a structured
             :class:`IssueBoard` (issues.json) that perf_eval files into
             and the implementer drains one issue at a time.
  "evolve": population-based evolutionary search.

The module is split into submodules by concern (``args`` for argparse wiring,
``config``/``inputs``/``remote``/``resume`` for the per-command preamble,
``loops`` for the request builder and runner). This package's
top level owns dispatch and re-exports the names those submodules and the
mode entries share.
"""

from __future__ import annotations

import sys
from collections.abc import Callable  # noqa: TC003  # tracked: #288
from dataclasses import dataclass
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
from entrypoints.cli.config import (
    _load_config_or_stub_default,
    _prepare_experiment_repository,
    _prepare_stub_agent_smoke_defaults,
    load_config_and_skills,
)
from entrypoints.cli.constants import _MODALITIES, _OUTER_LOOPS
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
)
from entrypoints.cli.resume import _resolve_resume_args
from vibesys.api import ConfigurationError, RunRequest, boot_trace
from vibesys.api.request import load_objective, with_operator_constraints

if TYPE_CHECKING:
    import argparse

# Aliases the tests reach for by name; the submodules use the imported names.
_load_objective = load_objective
_with_operator_constraints = with_operator_constraints

__all__ = [
    "_MODALITIES",
    "_OUTER_LOOPS",
    "CliInvocation",
    "_RunArgumentParser",
    "_add_common_args",
    "_apply_bundle_profiler_default",
    "_build_agent_parser",
    "_build_evolve_parser",
    "_build_plain_parser",
    "_configuration_error",
    "_extract_flag",
    "_extract_loop_selection",
    "_load_config_or_stub_default",
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
    "build_run_request",
    "dispatch",
    "load_config_and_skills",
    "parse_cli_invocation",
    "run_environment_spec_from_args",
]


@dataclass(frozen=True)
class CliInvocation:  # tracked: #288
    """One parsed, validated CLI invocation: its loop kind and namespace."""

    loop_kind: str
    args: argparse.Namespace


@dataclass(frozen=True)
class _LoopCommand:
    """Parser and validation for one CLI outer-loop selection."""

    build_parser: Callable[[], argparse.ArgumentParser]
    validate: Callable[[argparse.Namespace], None]


_LOOP_COMMANDS: dict[str, _LoopCommand] = {
    "agent": _LoopCommand(_build_agent_parser, _validate_agent),
    "profile-guided": _LoopCommand(_build_agent_parser, _validate_agent),
    "plain": _LoopCommand(_build_plain_parser, _validate_plain),
    "evolve": _LoopCommand(_build_evolve_parser, _validate_evolve),
}


def build_run_request(invocation: CliInvocation) -> RunRequest:
    """Build the ``RunRequest`` for one already-parsed CLI invocation.

    Runs the shared config, skills, repository, environment, and descriptor
    preamble without starting the run.
    """
    return _build_run_request(invocation.args)


def _explicit_cli_dests(
    parser: argparse.ArgumentParser,
    argv: list[str],
) -> frozenset[str]:
    """Return parser destinations whose option strings occur in *argv*."""
    destinations = {
        option: action.dest
        for action in parser._actions  # noqa: SLF001  # argparse exposes no public lookup
        for option in action.option_strings
    }
    return frozenset(
        destinations[option]
        for token in argv
        if token.startswith("-") and (option := token.partition("=")[0]) in destinations
    )


def parse_cli_invocation(argv: list[str]) -> CliInvocation:
    """Parse and validate one invocation without printing or exiting."""
    argv = _prepare_stub_agent_smoke_defaults(argv)
    loop_kind, remaining = _extract_loop_selection(argv)
    command = _LOOP_COMMANDS[loop_kind]
    parser = command.build_parser()
    parser.prog = f"vibesys --outer-loop {loop_kind}"
    args = parser.parse_args(remaining)
    args.outer_loop = loop_kind
    args.explicit_cli_dests = _explicit_cli_dests(parser, remaining)
    _normalize_runs_dir(args)
    _resolve_resume_args(args, loop_kind=loop_kind)
    command.validate(args)
    return CliInvocation(loop_kind=loop_kind, args=args)


def dispatch(argv: list[str]) -> None:
    """Parse and run one headless VibeSys invocation."""
    if argv and argv[0] == "validate":
        _run_validate(argv[1:])
        return
    with boot_trace.span("dispatch"), boot_trace.span("parse_cli_invocation"):
        invocation = parse_cli_invocation(argv)
    _run_request(invocation.args)


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
    print(f"vibesys: {diagnostic.message}", file=sys.stderr)  # noqa: T201  # tracked: #288
    if diagnostic.usage:
        print(diagnostic.usage, file=sys.stderr)  # noqa: T201  # tracked: #288
    raise SystemExit(diagnostic.exit_code)
