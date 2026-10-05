"""Run bounds and deadline derived from the validated dynamic config, never from constants."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import Limits

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.strategy.api import DynamicConfig

# Core requires a finite deadline. An unset `[run] max_run_seconds` means unbounded,
# which is the largest finite double: no run clock reaches it and nothing adds to it.
UNBOUNDED_DEADLINE_AT = 1.7976931348623157e308


def run_deadline_at(max_run_seconds: int | None) -> float:
    """The deadline on the core run clock (which starts at 0), or `UNBOUNDED_DEADLINE_AT`."""
    return UNBOUNDED_DEADLINE_AT if max_run_seconds is None else float(max_run_seconds)


def limits_for(
    config: DynamicConfig, *, observe_interval: float, observe_backoff_cap: float
) -> Limits:
    """Bounds that fit the strategy's own budgets exactly.

    Every workstream is one admitted attempt, so attempts are `max_rounds * max_in_flight`
    and a refund (an unsupported profile) can return at most that many. A correction
    chain of depth `d` counts its first reply, so `max_corrections` corrections need
    `max_retries = max_corrections + 1`. Turn chains: one planner chain per workstream
    plus the first, and per workstream its paid turns, one judge and one resume per
    measurement submission; each chain is a first reply plus its corrections. The
    polling pace of a running measurement job comes from the run's evaluation config.
    """
    starts = config.start_budget
    chain = 1 + config.max_corrections
    submissions = config.max_input_measurement_attempts
    per_workstream = config.max_retries_per_round + 1 + submissions
    return Limits(
        max_attempts=starts,
        max_turns=chain * ((starts + 1) + starts * per_workstream),
        max_parallel=config.max_in_flight,
        max_retries=config.max_corrections + 1,
        max_refunds=starts,
        max_measurement_submissions=submissions,
        queue_allowance=config.queue_allowance_seconds,
        observe_interval=observe_interval,
        observe_backoff_cap=observe_backoff_cap,
    )
