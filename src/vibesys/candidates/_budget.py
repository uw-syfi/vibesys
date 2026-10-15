"""The run's bounds, from what a search spends and what the backend declared.

A search states its budget in one meaning per field (`RunBudget`); the backend's
declarations and core's own wait bounds come from the run (`Bounds`). `limits_for` is the one
place they become core's `Limits`, so no search copies the assembly.
"""

from typing import Protocol

from pydantic import Field

from vs_core.api import Count, Limits, RequestRetries, Value

# Core requires a finite deadline. An unset `[run] max_run_seconds` means unbounded, which is the
# largest finite double: no run clock reaches it and nothing adds to it.
UNBOUNDED_DEADLINE_AT = 1.7976931348623157e308


def run_deadline_at(max_run_seconds: int | None) -> float:
    """The deadline on the core run clock (which starts at 0), or `UNBOUNDED_DEADLINE_AT`."""
    return UNBOUNDED_DEADLINE_AT if max_run_seconds is None else float(max_run_seconds)


def turn_chain(*, corrections: int, lost_turns: int) -> int:
    """Turns one logical turn can take: its first reply, its corrections and its re-asks."""
    return 1 + corrections + lost_turns


class RunBudget(Value):
    """What a search may spend. One field per meaning; the values are the search's config."""

    attempts: Count
    """Attempts core may admit."""
    turns: Count
    """Agent turns in all, every chain counted."""
    parallel: int = Field(ge=1)
    """Attempts that hold a slot at once."""
    refunds: Count
    """Attempts core never admitted, which return their charge."""
    adoption_rounds: int = Field(ge=1)
    """AdoptRevision rounds per winner, the first included."""


class Bounds(Protocol):
    """What the run's backend and product config declare, read by `limits_for`."""

    @property
    def request_retries(self) -> RequestRetries:
        """How core re-sends a request the executor did not run."""
        ...

    @property
    def observe_interval_seconds(self) -> float:
        """Poll cadence of a running measurement job."""
        ...

    @property
    def observe_backoff_cap_seconds(self) -> float:
        """Longest wait after a poll that could not read the job."""
        ...

    @property
    def queue_wait_budget_seconds(self) -> float:
        """Longest wait of one measurement's job in its executor's queue."""
        ...

    @property
    def queue_notice_interval_seconds(self) -> float:
        """How often a job still queued is reported, in seconds of queue time."""
        ...

    @property
    def cancellation_bound_seconds(self) -> float:
        """Longest wait for a cancelled job to be confirmed gone."""
        ...

    @property
    def cleanup_bound_seconds(self) -> float:
        """Least time core gives its own cleanup once the deadline has passed."""
        ...

    @property
    def reconciliation_bound_seconds(self) -> float:
        """Longest wait to learn what became of a request core could not read."""
        ...


def limits_for(budget: RunBudget, bounds: Bounds) -> Limits:
    """Core's `Limits` for a search's budget under the run's declared bounds."""
    return Limits(
        max_attempts=budget.attempts,
        max_turns=budget.turns,
        max_parallel=budget.parallel,
        pool_capacities=(),
        max_refunds=budget.refunds,
        max_adoption_rounds=budget.adoption_rounds,
        request_retries=bounds.request_retries,
        cancellation_bound=float(bounds.cancellation_bound_seconds),
        cleanup_bound=float(bounds.cleanup_bound_seconds),
        reconciliation_bound=float(bounds.reconciliation_bound_seconds),
        observe_interval=float(bounds.observe_interval_seconds),
        observe_backoff_cap=float(bounds.observe_backoff_cap_seconds),
        queue_wait_budget=float(bounds.queue_wait_budget_seconds),
        queue_notice_interval=float(bounds.queue_notice_interval_seconds),
    )
