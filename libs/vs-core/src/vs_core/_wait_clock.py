"""A limit on measurement work counts from its job's start: time a job spends queued is free.

Measurement work has a limit. An agent that waits on its own measurement states it when it
submits: core answers the call with the submission's deadline (``AgentCall.deadline_at``,
the call time plus the limit, capped at the run deadline), and the agent's wait
(``Continuation.deadline_at``) names it. A strategy's ``Measure`` counts its limit from the
submission's dispatch, and the submission's record (``RequestRecord.until``) stays open
until the job ends; a record whose limit passes is ended and its job cancelled. On a
contended cluster the job may then wait hours in a scheduler queue before it runs. That
time is the cluster's, not the work's, so neither limit charges it:

- A job is *queued* while its latest fact says it is pending in a queue and it has not
  ended (``queued``). The interval from such a fact to the job's next fact is queue time,
  however the next fact reads: the job started somewhere inside it, and the interval is
  given to the queue so that no queue time is ever charged.
- Each queue interval moves the limit of the work that started the job later by its
  length (``charge_queue_time``), still capped at the run deadline: the deadline of every
  agent call that names the job's submission, and the ``until`` of the open record that
  started the job (a submission, or a registered operation). Each limit is therefore the
  stated one counted from the job's start, plus the few seconds before the job's first fact.
- While a job is queued, nothing it started has a limit: a wait on it cannot time out
  (``wait_deadline`` in ``_continuations``), nor can its record (``record_limit``). Only the
  run's own deadline and stop end it, by closing the scope and cancelling its jobs.

Only these values move; no persisted shape changes. A job no agent call submitted (a
registered job) keeps the deadline its wait states once it runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.evaluation import OwnedJob, RegisteredOwnedJob
from .types.records import RequestPhase

if TYPE_CHECKING:
    from .types.common import RequestId, ResourceId
    from .types.evaluation import AgentCall, EvaluationState
    from .types.kernel import CoreState, RunState
    from .types.records import RequestRecord

_OPEN = (RequestPhase.PREPARED, RequestPhase.DISPATCHED, RequestPhase.RECONCILING)


def queued(job: OwnedJob | RegisteredOwnedJob) -> bool:
    """Whether the job's latest fact says it waits in a queue: it has not started running.

    A fact that carries no progress leaves the earlier progress in place, so only progress
    of the latest observation counts; a job whose latest fact says nothing about its queue
    is charged, which keeps every wait bounded.
    """
    progress, observation = job.progress, job.observation
    return (
        not job.terminal
        and progress is not None
        and observation is not None
        and progress.state == "pending"
        and progress.observation_sequence == observation.sequence
    )


def submission_deadline(state: EvaluationState, job: OwnedJob | RegisteredOwnedJob) -> float | None:
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


def _source(job: OwnedJob | RegisteredOwnedJob) -> RequestId:
    """The request that started ``job``: its submission, or its registered operation."""
    return job.submission_id if isinstance(job, OwnedJob) else job.request_id


def _all_jobs(state: EvaluationState) -> tuple[OwnedJob | RegisteredOwnedJob, ...]:
    return (*state.jobs, *state.registered_jobs)


def record_limit(record: RequestRecord, evaluation: EvaluationState) -> float | None:
    """When ``record``'s limit ends it, or None while a job it started is queued."""
    if any(_source(job) == record.request_id and queued(job) for job in _all_jobs(evaluation)):
        return None
    return record.until.at


def _queue_time(before: EvaluationState, after: EvaluationState) -> dict[RequestId, float]:
    """Seconds of queue time each job ended in this change, by the request that started it."""
    earlier: dict[tuple[type, ResourceId], OwnedJob | RegisteredOwnedJob] = {
        (type(job), job.resource_id): job
        for job in _all_jobs(before)
        if job.resource_id is not None
    }
    charged: dict[RequestId, float] = {}
    for job in _all_jobs(after):
        previous = None if job.resource_id is None else earlier.get((type(job), job.resource_id))
        if previous is None or not queued(previous) or previous.observation is None:
            continue
        if (
            job.observation is None
            or job.observation.observed_at <= previous.observation.observed_at
        ):
            continue
        charged[_source(job)] = job.observation.observed_at - previous.observation.observed_at
    return charged


def _later(at: float, shift: float, run: RunState) -> float:
    """``at`` moved ``shift`` later, capped at the run deadline unless it was already past it."""
    return min(at + shift, max(run.deadline_at, at))


def charge_queue_time(before: CoreState, after: CoreState) -> CoreState:
    """``after`` with every limit moved later by the queue time this evaluation change ended.

    The kernel runs it after every evaluation change, so it sees each new fact about a job
    exactly once: a job that was queued at its previous fact spent the time up to this one
    in the queue. The agent calls and the open record that name the job's source move
    together, by the same interval.
    """
    charged = _queue_time(before.evaluation, after.evaluation)
    if not charged:
        return after
    run = after.run

    def call(row: AgentCall) -> AgentCall:
        shift = None if row.request_id is None else charged.get(row.request_id)
        if shift is None or row.deadline_at is None:
            return row
        return row.model_copy(update={"deadline_at": _later(row.deadline_at, shift, run)})

    def record(row: RequestRecord) -> RequestRecord:
        shift = charged.get(row.request_id)
        if shift is None or row.phase not in _OPEN:
            return row
        until = row.until.model_copy(update={"at": _later(row.until.at, shift, run)})
        return row.model_copy(update={"until": until})

    evaluation = after.evaluation.model_copy(
        update={"agent_calls": tuple(call(row) for row in after.evaluation.agent_calls)}
    )
    records = after.records.model_copy(
        update={"records": tuple(record(row) for row in after.records.records)}
    )
    return after.model_copy(update={"evaluation": evaluation, "records": records})
