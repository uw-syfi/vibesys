"""A wait's run limit counts from its job's start: time a job spends queued is not charged.

An agent that waits on its own measurement suspends its turn until the job ends or the
wait's limit passes. The limit is stated when the agent submits: core answers the call
with the submission's deadline (``AgentCall.deadline_at``, the call time plus the limit,
capped at the run deadline), and the agent's wait (``Continuation.deadline_at``) names it.
On a contended cluster the job may then wait hours in a scheduler queue before it runs.
That time is the cluster's, not the work's, so it is not charged:

- A job is *queued* while its latest fact says it is pending in a queue and it has not
  ended (``queued``). The interval from such a fact to the job's next fact is queue time,
  however the next fact reads: the job started somewhere inside it, and the interval is
  given to the queue so that no queue time is ever charged.
- Each queue interval moves the deadline of every agent call that names the job's
  submission later by its length (``charge_queue_time``), still capped at the run deadline.
  The deadline is therefore the stated limit counted from the job's start, plus the few
  seconds between the call and the job's first fact.
- While any unfinished job of a wait is queued, the wait has no deadline: only the run's
  own deadline and stop end it, by closing the scope and cancelling its jobs.

Only these values move; no persisted shape changes. A job no agent call submitted (a
registered job) keeps the deadline its wait states once it runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.evaluation import OwnedJob

if TYPE_CHECKING:
    from .types.common import RequestId, ResourceId
    from .types.evaluation import AgentCall, EvaluationState, RegisteredOwnedJob
    from .types.kernel import RunState


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


def _queue_time(before: EvaluationState, after: EvaluationState) -> dict[RequestId, float]:
    """Seconds of queue time each submission's job ended in this change, by submission."""
    earlier: dict[ResourceId, OwnedJob] = {
        job.resource_id: job for job in before.jobs if job.resource_id is not None
    }
    charged: dict[RequestId, float] = {}
    for job in after.jobs:
        previous = earlier.get(job.resource_id) if job.resource_id is not None else None
        if previous is None or not queued(previous) or previous.observation is None:
            continue
        if (
            job.observation is None
            or job.observation.observed_at <= previous.observation.observed_at
        ):
            continue
        charged[job.submission_id] = job.observation.observed_at - previous.observation.observed_at
    return charged


def charge_queue_time(
    before: EvaluationState, after: EvaluationState, run: RunState
) -> EvaluationState:
    """``after`` with each agent call's deadline moved later by the queue time this change ended.

    Run on every evaluation change, it sees each new fact about a job exactly once: a job
    that was queued at its previous fact spent the time up to this one in the queue.
    """
    charged = _queue_time(before, after)
    if not charged:
        return after

    def moved(call: AgentCall) -> AgentCall:
        shift = None if call.request_id is None else charged.get(call.request_id)
        if shift is None or call.deadline_at is None:
            return call
        deadline = min(call.deadline_at + shift, max(run.deadline_at, call.deadline_at))
        return call.model_copy(update={"deadline_at": deadline})

    return after.model_copy(update={"agent_calls": tuple(moved(c) for c in after.agent_calls)})
