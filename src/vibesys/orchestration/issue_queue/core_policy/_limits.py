"""Run bounds and deadline derived from the validated issue-queue config, never from constants."""

from vibesys.orchestration.issue_queue.strategy.api import IssueQueueConfig
from vs_core.api import DEFAULT_MAX_MEASUREMENT_SUBMISSIONS, Limits

# Core requires a finite deadline. An unset `[run] max_run_seconds` means unbounded, which is the
# largest finite double: no run clock reaches it and nothing adds to it.
UNBOUNDED_DEADLINE_AT = 1.7976931348623157e308


def run_deadline_at(max_run_seconds: int | None) -> float:
    """The deadline on the core run clock (which starts at 0), or `UNBOUNDED_DEADLINE_AT`."""
    return UNBOUNDED_DEADLINE_AT if max_run_seconds is None else float(max_run_seconds)


def limits_for(
    config: IssueQueueConfig,
    *,
    queue_allowance: float,
    observe_interval: float,
    observe_backoff_cap: float,
) -> Limits:
    """Bounds that fit the strategy's own budgets exactly.

    Each round works at most ``max_issues_per_round`` issues and runs one evaluation, and
    every job is one attempt, so attempts are rounds times that plus one. An issue has a
    logical implementer turn and a judge turn for each of its ``max_attempts_per_issue``
    attempts, an evaluation one; each logical turn is a chain of its first reply, its
    corrections and its asks after a lost turn, and may also be resumed once for each
    measurement its role submits.
    """
    rounds = config.max_rounds
    submissions = DEFAULT_MAX_MEASUREMENT_SUBMISSIONS
    per_issue = 2 * config.max_attempts_per_issue + submissions
    per_round = config.max_issues_per_round * per_issue + (1 + submissions)
    extra = config.max_corrections + config.max_turn_drops
    logical = rounds * (config.max_issues_per_round * 2 * config.max_attempts_per_issue + 1)
    return Limits(
        max_attempts=config.max_attempts,
        max_turns=config.chain * rounds * per_round,
        max_parallel=1,
        max_retries=extra * logical + 1,
        max_refunds=config.max_attempts,
        max_measurement_submissions=submissions,
        queue_allowance=queue_allowance,
        observe_interval=observe_interval,
        observe_backoff_cap=observe_backoff_cap,
    )
