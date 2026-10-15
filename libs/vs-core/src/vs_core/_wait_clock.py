"""A limit on measurement work counts from its job's start: queue time is data, never charged.

Measurement work has a limit. An agent that waits on its own measurement states it when it
submits: core answers the call with the submission's deadline (``AgentCall.deadline_at``),
and the agent's wait (``Continuation.deadline_at``) names a deadline. A strategy's
``Measure`` counts its limit from the submission's dispatch (``RequestRecord.until``), and a
registered operation from its own. On a contended cluster the job may then wait hours in a
scheduler queue before it runs. That time is the cluster's, not the work's.

Every stated limit stays as it was stated. Each job instead records the queue time its facts
earned (``queue_credit``), and every limit on work tied to the job is derived from it here:

- **Credit.** The interval from a fact that says the job is pending (``pending``) to the
  job's next fact earns its length, but never more than ``observe_backoff_cap``, the longest
  delay between two polls (``credited``). A pending fact that keeps being refreshed earns all
  its queue time; a stale one (the scheduler stopped answering) earns at most that much, so a
  poll outage is never credited as queue time and cannot extend a limit.
- **Bound.** A job's work ends at its stated limit plus its credit. While its latest fact is
  pending it may still earn up to the cap, so its bound is that much later; and its queue
  budget (``queue_wait_budget``) ends it once its credit and its current pending interval
  reach the budget. A limit that queue credit carries past the run deadline ends with the
  run (``job_bound``).
- **Waits and records.** Each unfinished job of a wait or a record has its own bound; the
  wait or record ends at the earliest one (``wait_bound``, ``record_bound``).

Every bound is a time (``Bound``), never absent, so an unbounded wait cannot be represented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .types.evaluation import OwnedJob, RegisteredOwnedJob
from .types.job_observations import WaitEnd

if TYPE_CHECKING:
    from .types.common import Limits, Observation, RequestId
    from .types.evaluation import Continuation, EvaluationState
    from .types.kernel import RunState
    from .types.records import RequestRecord

type Job = OwnedJob | RegisteredOwnedJob


@dataclass(frozen=True)
class Bound:
    """When a wait on a job ends, and which rule set that time. Never absent."""

    at: float
    by: WaitEnd


def _earliest(bounds: tuple[Bound, ...], default: Bound) -> Bound:
    return min(bounds, key=lambda bound: bound.at, default=default)


def pending(job: Job) -> bool:
    """Whether the job's latest fact says it waits in a queue: it has not started running.

    A fact that carries no progress leaves the earlier progress in place, so only progress
    of the latest observation counts; a job whose latest fact says nothing about its queue
    earns no credit, which keeps every wait bounded.
    """
    progress, observation = job.progress, job.observation
    return (
        not job.terminal
        and progress is not None
        and observation is not None
        and progress.state == "pending"
        and progress.observation_sequence == observation.sequence
    )


def credited(job: Job, observation: Observation, limits: Limits) -> float:
    """``job``'s queue credit once it takes ``observation``, its next fact.

    The interval since a pending fact earns its length, capped at ``observe_backoff_cap``:
    a scheduler that answers polls never leaves a longer gap, so a longer one is an outage,
    not queue time.
    """
    previous = job.observation
    if not pending(job) or previous is None or observation.observed_at <= previous.observed_at:
        return job.queue_credit
    earned = min(observation.observed_at - previous.observed_at, limits.observe_backoff_cap)
    return job.queue_credit + earned


def source(job: Job) -> RequestId:
    """The request that started ``job``: its submission, or its registered operation."""
    return job.submission_id if isinstance(job, OwnedJob) else job.request_id


def queue_budget_left(job: Job, limits: Limits) -> float:
    """Queue time ``job`` may still be credited before its queue budget is spent."""
    return max(limits.queue_wait_budget - job.queue_credit, 0.0)


def job_bound(job: Job, stated: float, run: RunState) -> Bound:
    """When work on ``job`` with the stated limit ``stated`` ends, from the job's facts.

    The limit plus the job's credit; while its latest fact is pending, plus what that fact
    may still earn (the cap), or sooner when its queue budget runs out. A limit that queue
    credit carries past the run deadline ends with the run.
    """
    limits = run.limits
    bound = Bound(at=stated + job.queue_credit, by=WaitEnd.LIMIT)
    if pending(job) and job.observation is not None:
        queued_since = job.observation.observed_at
        bound = _earliest(
            (
                Bound(at=bound.at + limits.observe_backoff_cap, by=WaitEnd.LIMIT),
                Bound(at=queued_since + queue_budget_left(job, limits), by=WaitEnd.QUEUE_BUDGET),
            ),
            bound,
        )
    ceiling = max(run.deadline_at, stated)
    return bound if bound.at <= ceiling else Bound(at=ceiling, by=WaitEnd.RUN_END)


def _call_deadline(state: EvaluationState, job: Job) -> float | None:
    """The deadline of the agent call that allocated ``job``'s submission, or None."""
    if not isinstance(job, OwnedJob):
        return None
    return next(
        (
            call.deadline_at
            for call in state.agent_calls
            if call.request_id == job.submission_id and not call.repeat
        ),
        None,
    )


def wait_bound(
    state: EvaluationState, run: RunState, wait: Continuation, jobs: tuple[Job, ...]
) -> Bound:
    """When ``wait`` on its unfinished ``jobs`` ends: at the earliest of their bounds.

    Each job's stated limit is the wait's deadline, or the deadline of the agent call that
    allocated the job's submission when that is later; each job adds its own queue credit,
    so a sibling's queue time never extends a running job's limit.
    """
    bounds = tuple(
        job_bound(job, max(wait.deadline_at, _call_deadline(state, job) or 0.0), run)
        for job in jobs
    )
    return _earliest(bounds, Bound(at=wait.deadline_at, by=WaitEnd.LIMIT))


def record_bound(record: RequestRecord, evaluation: EvaluationState, run: RunState) -> Bound:
    """When ``record``'s limit ends it: at the earliest bound of an unfinished job it started.

    A record that started no job (or whose jobs all ended) ends at its stated ``until``.
    """
    bounds = tuple(
        job_bound(job, record.until.at, run)
        for job in (*evaluation.jobs, *evaluation.registered_jobs)
        if source(job) == record.request_id and not job.terminal
    )
    return _earliest(bounds, Bound(at=record.until.at, by=WaitEnd.LIMIT))


def notice_due(job: Job, before: float, limits: Limits) -> bool:
    """Whether pending ``job``'s credit crossed a multiple of ``queue_notice_interval``.

    ``before`` is its credit before its latest fact, so each interval is reported once.
    """
    interval = limits.queue_notice_interval
    return pending(job) and job.queue_credit // interval > before // interval
