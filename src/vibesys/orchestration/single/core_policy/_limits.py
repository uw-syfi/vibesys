"""Run bounds and deadline derived from the validated single config, never from constants."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import Limits

if TYPE_CHECKING:
    from vibesys.orchestration.single.strategy.api import SingleConfig

# Core requires a finite deadline. An unset `[run] max_run_seconds` means unbounded, which is the
# largest finite double: no run clock reaches it and nothing adds to it.
UNBOUNDED_DEADLINE_AT = 1.7976931348623157e308


def run_deadline_at(max_run_seconds: int | None) -> float:
    """The deadline on the core run clock (which starts at 0), or `UNBOUNDED_DEADLINE_AT`."""
    return UNBOUNDED_DEADLINE_AT if max_run_seconds is None else float(max_run_seconds)


def limits_for(
    config: SingleConfig, *, observe_interval: float, observe_backoff_cap: float
) -> Limits:
    """Bounds that fit the strategy's own budgets exactly.

    One round is one admitted attempt, run serially, so attempts are `max_rounds` and a refund
    (an attempt core never admitted) can return at most that many. A chain of turns counts its
    first reply, `max_corrections` corrections and `max_turn_drops` asks after a lost turn. Per
    round: the designer's chain, `max_retries_per_round` paid chains and one resume per
    measurement submission; the designer is asked at most once per round.
    """
    chain = 1 + config.max_corrections + config.max_turn_drops
    submissions = config.max_input_measurement_attempts
    per_round = 1 + config.max_retries_per_round + submissions
    return Limits(
        max_attempts=config.max_rounds,
        max_turns=chain * config.max_rounds * per_round,
        max_parallel=1,
        max_retries=config.max_corrections + config.max_turn_drops + 1,
        max_refunds=config.max_rounds,
        max_measurement_submissions=submissions,
        queue_allowance=config.queue_allowance_seconds,
        observe_interval=observe_interval,
        observe_backoff_cap=observe_backoff_cap,
    )
