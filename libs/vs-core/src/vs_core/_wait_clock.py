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
- **Downtime.** While no host runs, nothing waits: the run clock excludes downtime
  (``Record.clock``), so a limit never counts it. It is never also queue time: the part of an
  interval that overlaps a downtime earns no credit and spends no queue budget.
- **Bound.** A job's work ends at its stated limit plus its credit. While its latest fact is
  pending it may still earn up to the cap, so its bound is that much later; and its queue
  budget (``queue_wait_budget``) ends it once its credit and its current pending interval
  reach the budget. A limit that queue credit carries past the run deadline ends with the
  run (``job_bound``).
- **Waits and records.** Each unfinished job of a wait or a record has its own bound. A
  record ends at the earliest one (``record_bound``). A wait ends a job whose queue budget
  ran out on its own and goes on with the rest; it ends at the earliest other bound, or once
  every job has ended (``wait_bounds``, ``_continuations``).

Every bound is a time (``Bound``), never absent, so an unbounded wait cannot be represented.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from ._registry import ContractError
from ._turn_holds import held_limit
from .types.evaluation import OwnedJob, RegisteredOwnedJob
from .types.job_observations import WaitEnd

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .types.common import Limits, Observation, RequestId
    from .types.evaluation import Continuation, EvaluationState
    from .types.kernel import RunState
    from .types.records import CapacityHold, HostEpoch, RecordsState, RequestRecord
    from .types.until import Until

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


def _downtime_within(hosts: Sequence[HostEpoch], start: float, end: float) -> float:
    """The part of ``[start, end]`` no host was alive, from the host log (``_host_epochs``)."""
    return sum(
        max(min(later.started_at, end) - max(earlier.alive_at, start), 0.0)
        for earlier, later in pairwise(hosts)
    )


def credited(
    job: Job, observation: Observation, limits: Limits, hosts: Sequence[HostEpoch]
) -> float:
    """``job``'s queue credit once it takes ``observation``, its next fact.

    The interval since a pending fact earns its length, capped at ``observe_backoff_cap``:
    a scheduler that answers polls never leaves a longer gap, so a longer one is an outage,
    not queue time. Host downtime in the interval earns nothing: the wait's or record's
    limit is already moved past it.
    """
    previous = job.observation
    if not pending(job) or previous is None or observation.observed_at <= previous.observed_at:
        return job.queue_credit
    start, end = previous.observed_at, observation.observed_at
    queued = end - start - _downtime_within(hosts, start, end)
    return job.queue_credit + min(max(queued, 0.0), limits.observe_backoff_cap)


def source(job: Job) -> RequestId:
    """The request that started ``job``: its submission, or its registered operation."""
    return job.submission_id if isinstance(job, OwnedJob) else job.request_id


def queue_budget_left(job: Job, limits: Limits) -> float:
    """Queue time ``job`` may still be credited before its queue budget is spent."""
    return max(limits.queue_wait_budget - job.queue_credit, 0.0)


def job_bound(job: Job, stated: float, run: RunState, hosts: Sequence[HostEpoch]) -> Bound:
    """When work on ``job`` with the stated limit ``stated`` ends, from the job's facts.

    The limit plus the job's credit; while its latest fact is pending, plus what that fact
    may still earn (the cap), or sooner when its queue budget runs out. Host downtime since
    the pending fact spends no budget. A limit that queue credit carries past the run
    deadline ends with the run.
    """
    limits = run.limits
    bound = Bound(at=stated + job.queue_credit, by=WaitEnd.LIMIT)
    if pending(job) and job.observation is not None and bound.at > job.observation.observed_at:
        # The pending fact came before the limit passed, so the time since it is queue time,
        # up to the cap; one that came after it cannot revive the limit.
        queued_since = job.observation.observed_at
        down = _downtime_within(hosts, queued_since, run.now_at)
        bound = _earliest(
            (
                Bound(at=bound.at + limits.observe_backoff_cap, by=WaitEnd.LIMIT),
                # When the budget is (or was) spent: a fact after that instant leaves it put.
                Bound(
                    at=queued_since + down + limits.queue_wait_budget - job.queue_credit,
                    by=WaitEnd.QUEUE_BUDGET,
                ),
            ),
            bound,
        )
    ceiling = max(run.deadline_at, stated)
    return bound if bound.at <= ceiling else Bound(at=ceiling, by=WaitEnd.RUN_END)


def job_step_until(run: RunState, until: Until) -> Until:
    """The limit of a job step the recovery loop starts: the run's queue budget, then its limit.

    A job's limit counts from its start, and queue time is the cluster's, not the work's. A
    step is one request the loop hands to a runner whole, and core sees its job only at its end:
    no fact says the job still waits in a queue (or for the root proof a run environment holds
    its work for), so none can credit that time (``credited``). The step instead gets the
    whole queue budget before its limit counts, and the executor bounds the job once it runs (the
    scheduler's time limit, a stage's timeout). What the step's own limit still ends is a job
    that never starts: after the queue budget a held or queued job is the cluster's failure, not
    a wait. A limit never passes the run deadline, as a limit core credits never does
    (``job_bound``); a run past its deadline gets no more time for its cleanup.
    """
    if run.deadline_at <= run.now_at:
        return until
    at = min(until.at + run.limits.queue_wait_budget, run.deadline_at)
    return until.model_copy(update={"at": max(at, until.at)})


def _submission_limit(evaluation: EvaluationState, records: RecordsState, job: Job) -> float | None:
    """The limit of ``job``'s submission when an agent's call allocated it, else None.

    The submission's record holds that limit (``until``): the call answered the agent with
    the same value, and the run clock excludes downtime, so a wait opened after a restart counts
    from the same limit as the record does.
    """
    if not isinstance(job, OwnedJob) or not any(
        call.request_id == job.submission_id and not call.repeat for call in evaluation.agent_calls
    ):
        return None
    record = records.record(job.submission_id)
    if record is None:
        raise ContractError(("jobs", "submission_id"), "an agent's submission has no record")
    return record.until.at


def wait_bounds(
    evaluation: EvaluationState,
    records: RecordsState,
    run: RunState,
    wait: Continuation,
    jobs: tuple[Job, ...],
) -> tuple[Bound, ...]:
    """The bound of each of ``wait``'s ``jobs``, in order: when the wait is done with it.

    Each job's stated limit is the later of two. One is the wait's deadline, moved past the
    host downtime the wait was credited (capped at the run deadline, as a record's moved
    limit is). The other is the limit of the job's submission when an agent's call allocated
    it, read from the submission's record, which a restart moves the same way. Each job adds
    its own queue credit, so a sibling's queue time never extends a running job's limit.
    """
    moved = wait.deadline_at + wait.downtime_credit
    own = min(moved, run.deadline_at) if wait.deadline_at <= run.deadline_at else moved

    def stated(job: Job) -> float:
        return max(own, _submission_limit(evaluation, records, job) or 0.0)

    return tuple(job_bound(job, stated(job), run, records.hosts) for job in jobs)


def turn_bound(record: RequestRecord, run: RunState) -> Bound:
    """When ``record``'s own limit ends it: its stated limit plus the capacity hold it is credited.

    The rules are ``_turn_holds.held_limit``'s; a bound past the run deadline ends with the run,
    and a parked hold lasts until the run ends. A record with no hold ends at its ``until``.
    """
    return held_turn_bound(record.until.at, record.capacity_hold, run.deadline_at)


def held_turn_bound(stated: float, hold: CapacityHold | None, run_deadline: float | None) -> Bound:
    """The one turn-bound rule: ``stated`` plus its credited hold, capped at the run's end.

    ``run_deadline`` is None for a run with no deadline, whose credited bound is never capped.
    """
    if hold is None:
        return Bound(at=stated, by=WaitEnd.LIMIT)
    at = held_limit(stated, hold)
    if run_deadline is None:
        return Bound(at=at, by=WaitEnd.LIMIT)
    ceiling = Bound(at=max(run_deadline, stated), by=WaitEnd.RUN_END)
    return Bound(at=at, by=WaitEnd.LIMIT) if at <= ceiling.at else ceiling


def record_bound(
    record: RequestRecord,
    evaluation: EvaluationState,
    run: RunState,
    hosts: Sequence[HostEpoch],
) -> Bound:
    """When ``record``'s limit ends it: at the earliest bound of an unfinished job it started.

    A record that started no job (or whose jobs all ended) ends at its own bound: its stated
    ``until``, extended by the capacity hold of the turn it dispatches (``turn_bound``).
    """
    bounds = tuple(
        job_bound(job, record.until.at, run, hosts)
        for job in (*evaluation.jobs, *evaluation.registered_jobs)
        if source(job) == record.request_id and not job.terminal
    )
    return _earliest(bounds, turn_bound(record, run))


def notice_due(job: Job, before: float, limits: Limits) -> bool:
    """Whether pending ``job``'s credit crossed a multiple of ``queue_notice_interval``.

    ``before`` is its credit before its latest fact, so each interval is reported once.
    """
    interval = limits.queue_notice_interval
    return pending(job) and job.queue_credit // interval > before // interval
