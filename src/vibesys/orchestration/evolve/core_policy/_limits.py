"""Run bounds and deadline derived from the validated evolve config, never from constants."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import Limits

if TYPE_CHECKING:
    from vibesys.orchestration.evolve.strategy.api import EvolveConfig

# Core requires a finite deadline. An unset `[run] max_run_seconds` means unbounded, which is the
# largest finite double: no run clock reaches it and nothing adds to it.
UNBOUNDED_DEADLINE_AT = 1.7976931348623157e308


def run_deadline_at(max_run_seconds: int | None) -> float:
    """The deadline on the core run clock (which starts at 0), or `UNBOUNDED_DEADLINE_AT`."""
    return UNBOUNDED_DEADLINE_AT if max_run_seconds is None else float(max_run_seconds)


def limits_for(
    config: EvolveConfig, *, observe_interval: float, observe_backoff_cap: float
) -> Limits:
    """Bounds that fit the strategy's own budgets exactly.

    One child is one attempt, and a run starts at most every cold start plus every child of every
    generation, so attempts are `total_attempts` and a refund (an attempt core never admitted) can
    return at most that many. A chain of turns counts its first reply, `max_corrections`
    corrections and `max_turn_drops` asks after a lost turn. Per attempt: the mutator, judge
    and profiler chains, and one resume per measurement submission for each of the two roles that
    may wait. At most `max_parallelism` attempts hold a slot at once.
    """
    chain = 1 + config.max_corrections + config.max_turn_drops
    submissions = config.max_input_measurement_attempts
    per_attempt = 3 + 2 * submissions
    attempts = config.total_attempts
    return Limits(
        max_attempts=attempts,
        max_turns=chain * attempts * per_attempt,
        max_parallel=config.max_parallelism,
        max_retries=config.max_corrections + config.max_turn_drops + 1,
        max_refunds=attempts,
        max_measurement_submissions=submissions,
        queue_allowance=config.queue_allowance_seconds,
        observe_interval=observe_interval,
        observe_backoff_cap=observe_backoff_cap,
    )
