"""The task script's command line when the task brings no system of its own (``main``).

    bench.py check                              exit 0 when the check passes
    bench.py benchmark --vs-output FILE [--seed N]   one sample, result-protocol-2 records
    bench.py profile [--seed N]                 the standard profile load, never scored

The system under test is already running (vibesys-cli starts the candidate);
domains reach it through their own helpers. A mode the task did not define
exits 2 naming it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys_evaluator._private.run import new_run
from vibesys_evaluator._private.sample import (
    Mode,
    benchmark_sample,
    log,
    unscored_sample,
    with_signals,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from vibesys_evaluator._private.results import Metric
    from vibesys_evaluator._private.run import Run
    from vibesys_evaluator._private.sample import Sample

#: Exit status of a usage error (argparse's), also for a mode the task does not define.
USAGE = 2


@dataclass(frozen=True, kw_only=True)
class Task:
    """Everything the task script passes to ``main``."""

    metrics: Mapping[str, Metric]
    benchmark: Callable[[Run], None]
    check: Callable[[Run], str | None] | None
    profile: Callable[[Run], None] | None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bench.py", description="A vibesys-evaluator task.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(Mode.CHECK, help="check the system's correctness (exit 0 = pass)")
    bench = commands.add_parser(Mode.BENCHMARK, help="measure one sample")
    bench.add_argument("--vs-output", type=Path, required=True)
    bench.add_argument("--seed", type=int, default=None, help="replay the sample with this seed")
    profile = commands.add_parser(Mode.PROFILE, help="run the profile load (never scored)")
    profile.add_argument("--seed", type=int, default=None)
    return parser


def run(task: Task, argv: Sequence[str], *, env: Mapping[str, str]) -> int:
    """Run one command of the task script; returns the exit status."""
    args = _parser().parse_args(list(argv))
    mode = Mode(args.command)
    if mode is Mode.BENCHMARK:
        return with_signals(
            lambda: benchmark_sample(
                metrics=task.metrics,
                output=args.vs_output,
                seed_flag=args.seed,
                env=env,
                body=lambda sample: task.benchmark(new_run(sample)),
            )
        )
    seed = getattr(args, "seed", None)
    body = _unscored_body(task, mode)
    if body is None:
        log(f"this task defines no {mode}: pass {mode}= to main(...)")
        return USAGE
    return with_signals(lambda: unscored_sample(mode, seed_flag=seed, env=env, body=body))


def _unscored_body(task: Task, mode: Mode) -> Callable[[Sample], str | None] | None:
    """What a check or profile sample runs, or None when the task does not define the mode."""
    check, profile = task.check, task.profile
    if mode is Mode.CHECK and check is not None:

        def checked(sample: Sample) -> str | None:
            return check(new_run(sample))

        return checked
    if mode is Mode.PROFILE and profile is not None:

        def profiled(sample: Sample) -> None:
            profile(new_run(sample))

        return profiled
    return None
