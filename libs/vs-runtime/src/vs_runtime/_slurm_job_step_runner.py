"""A step that is a Slurm evaluation job, run again by finding the job it already started.

The Slurm job outlives the host by design: the evaluation ledger on the cluster names it by
the request's idempotency key, a hash of the content the job reads and its spec
(``vs_slurm.ledger.evaluation_key``). A later process asks for the same step, so the executor
attaches to the job the earlier attempt submitted instead of submitting again. ``clear``
therefore removes nothing: it computes the step's key and asks the ledger whether an earlier
attempt left a request.

The key names the content, not the tag, and not the run: two steps of one content and one spec,
of one run or of two runs on one cluster, share one job, and a step that finds the job another
step submitted attaches to it, as it would have if it had run first. What a run may end of a
shared job is the ledger's rule, not this runner's: ``clear`` cancels nothing of a job a run
still wants, and a cancel goes through ``relinquish``, which drops this run's lease and leaves
the job to every other run that holds one. The one job ``clear`` ends is the job of a withdrawn
attempt, which no run wants by definition.

A job this runner cancelled is never attached to. The cancel interrupted the attempt (a pause,
a drain deadline) and left no result, so the next attempt has to run the step: it takes the
next handle of the tag, and the ledger, which holds a withdrawn attempt final, claims a new job.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from vs_evaluation.api import EvaluationState, ExecutorRejectedError, PollPhase
from vs_runtime._evaluation_jobs import RejectedPlanError, handle_for
from vs_runtime._job_step_run import JobStepRun, evaluation_request, submission_of
from vs_runtime._step_runner import Adopted, Cleared, ClearUnavailableError
from vs_slurm.api import (
    ClusterObservation,
    ClusterUnknown,
    LedgerError,
    LedgerState,
    live_observation,
)

if TYPE_CHECKING:
    from vs_core.api import Request, RequestId, SubmitMeasurement
    from vs_core.api.recovery import StepResult
    from vs_evaluation.api import ExecutorPoll
    from vs_runtime._semantic_slurm_evaluation import SemanticSlurmEvaluationExecutor
    from vs_runtime._step_runner import CancelSignal
    from vs_sim.api import BlockingRunner, Clock
    from vs_slurm.api import Cluster, EvaluationLedger, LedgerEntry

_BACKEND = "slurm"
_SHOWS_A_JOB = frozenset({PollPhase.QUEUED, PollPhase.RUNNING, PollPhase.ENDED})
"""The phases of a poll that prove the tag submitted a job, whatever the workspace or the ledger show."""


@dataclass(frozen=True)
class SlurmScheduler:
    """What a Slurm step runner asks of the cluster beside its executor."""

    ledger: EvaluationLedger
    """The cluster's evaluation ledger, through which the executor attaches."""
    cluster: Cluster
    """The scheduler the executor submits to: asked whether a withdrawn attempt's job has ended."""
    blocking: BlockingRunner
    """Carries the ledger's and the cluster's blocking reads off the event loop."""


class SlurmJobStepRunner:
    """Runs ``SubmitMeasurement`` steps on the Slurm executor, which submits each job once."""

    def __init__(
        self,
        executor: SemanticSlurmEvaluationExecutor,
        scheduler: SlurmScheduler,
        *,
        clock: Clock,
        poll_interval_s: float,
    ) -> None:
        """Run on *executor*, which attaches through the *scheduler*'s ledger; poll every interval."""
        self._executor = executor
        self._ledger = scheduler.ledger
        self._cluster = scheduler.cluster
        self._blocking = scheduler.blocking
        self._job = JobStepRun(executor, clock=clock, poll_interval_s=poll_interval_s)

    async def run(
        self,
        step: Request,
        tag: RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        """Run the step's job, or attach to the one an earlier attempt submitted, and wait.

        A step that reuses an earlier submission launches nothing and reads that job's result.
        """
        del attempt  # the job is found by the step's key, so no attempt number names it
        submission = submission_of(step)
        if submission.reuses is not None:
            source, _ = await self._current(submission.reuses)
            return await self._job.read_ended(step, tag, source)
        handle, _ = await self._current(tag)
        return await self._job.run(step, tag, handle, deadline_at, cancel)

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """Adopt the job an earlier attempt left, or report that it left none.

        The tag's own handle is asked first: a job this step submitted is on record under it,
        whatever the workspace shows now. ``Adopted(None)`` for one that runs or ended, so the
        next ``run`` attaches (an ended job returns its collected result without new work); a
        handle on record whose state the executor cannot yet read is not "never ran" either: if
        the plan cannot be keyed then, the step is ``ClearUnavailableError``. With no job of the tag,
        the ledger decides whether another step of the same content left one, because it alone
        proves whether a job was submitted, also in the window where the executor has not yet
        saved the job's handle: ``Adopted(None)`` for a request that is claimed, queued, running
        or ended. A request the ledger holds withdrawn left no job to take over, but its job
        was cancelled without waiting for it to end, so the scheduler is asked and the step is
        cleared only once that job is gone. A step that cannot run, or that only reads another's
        result, left nothing. A result is not built here: the step's observation comes from the
        job's own end. A ledger or a scheduler that cannot be read raises
        ``ClearUnavailableError``: neither a job nor its absence is proven.
        """
        submission = submission_of(step)
        if submission.reuses is not None:
            return Cleared()
        try:
            _, polled = await self._current(tag)
        except LedgerError as error:
            raise ClearUnavailableError(tag, _BACKEND) from error
        if polled.phase in _SHOWS_A_JOB:
            return Adopted(None)
        try:
            key = await self._key_of(submission, tag)
            entry = await self._blocking.run(self._ledger.read, key)
        except (RejectedPlanError, ExecutorRejectedError) as error:
            if polled.phase is PollPhase.UNKNOWN:
                # A handle is on record for the tag: it submitted something. That the workspace
                # no longer holds the candidate's commit says nothing about that job.
                raise ClearUnavailableError(tag, _BACKEND) from error
            return Cleared()
        except LedgerError as error:
            raise ClearUnavailableError(tag, _BACKEND) from error
        if entry is None:
            return Cleared()
        if entry.record.state is LedgerState.WITHDRAWN:
            await self._job_of_withdrawn_ended(entry, tag)
            return Cleared()
        return Adopted(None)

    async def _job_of_withdrawn_ended(self, entry: LedgerEntry, tag: RequestId) -> None:
        """Return once the scheduler shows no live job of the withdrawn attempt, cancelling one it does.

        The withdrawal records the attempt after it asked for the cancel, not after the job
        ended, so a job can still run. A reading that cannot tell is not a job's absence.
        """
        operation = entry.record.operation_id
        live = await self._live_job(operation, tag)
        if live is None:
            return
        await self._blocking.run(partial(self._cluster.cancel, operation, observed=live))
        if await self._live_job(operation, tag) is not None:
            raise ClearUnavailableError(tag, "a withdrawn job that has not ended")

    async def _live_job(self, operation: str, tag: RequestId) -> ClusterObservation | None:
        """The queued or running job of *operation*, or ``None`` once the scheduler shows none."""
        observed = await self._blocking.run(self._cluster.inspect, operation)
        if isinstance(observed, ClusterUnknown):
            if observed.job_absent:
                return None
            raise ClearUnavailableError(tag, _BACKEND)
        return live_observation(observed)

    async def _key_of(self, submission: SubmitMeasurement, tag: RequestId) -> str:
        """The ledger key of the step's job; it covers neither the handle nor the deadline."""
        deadline_at = submission.plan.submitted_at + submission.plan.time_bound
        request = evaluation_request(submission, handle_for(tag), deadline_at=deadline_at)
        return await self._executor.ledger_key(request)

    async def _current(self, tag: RequestId) -> tuple[str, ExecutorPoll]:
        """The handle of the tag's current job and its poll: the first one not ended cancelled."""
        base = handle_for(tag)
        life = 0
        while True:
            handle = base if life == 0 else f"{base}-life-{life}"
            polled = await self._executor.poll(handle)
            if not _ended_cancelled(polled):
                return handle, polled
            life += 1


def _ended_cancelled(polled: ExecutorPoll) -> bool:
    return (
        polled.phase is PollPhase.ENDED
        and polled.terminal is not None
        and polled.terminal.state is EvaluationState.CANCELED
    )
