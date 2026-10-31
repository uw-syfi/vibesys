"""A step that is a Slurm evaluation job, run again by finding the job it already started.

The Slurm job outlives the host by design: the evaluation ledger on the cluster names it by
the request's idempotency key, a hash of the content the job reads and its spec
(``vs_slurm.ledger.evaluation_key``). A later process asks for the same step, so the executor
attaches to the job the earlier attempt submitted instead of submitting again. ``clear``
therefore removes nothing: it computes the step's key and asks the ledger whether an earlier
attempt left a request.

The key names the content, not the tag, so two steps of one content and one spec share one
job; a step that finds the job another step submitted attaches to it, as it would have if it
had run first.

A job this runner cancelled is never attached to. The cancel interrupted the attempt (a pause,
a drain deadline) and left no result, so the next attempt has to run the step: it takes the
next handle of the tag, and the ledger, which holds a withdrawn attempt final, claims a new job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_evaluation.api import EvaluationState, ExecutorRejectedError, PollPhase
from vs_runtime._evaluation_jobs import RejectedPlanError, handle_for
from vs_runtime._job_step_run import evaluation_request, submission_of
from vs_runtime._step_runner import Adopted, Cleared, ClearUnavailableError
from vs_slurm.api import LedgerError, LedgerState

if TYPE_CHECKING:
    from vs_core.api import Request, RequestId, SubmitMeasurement
    from vs_core.api.recovery import StepResult
    from vs_evaluation.api import ExecutorPoll
    from vs_runtime._job_step_run import JobStepRun
    from vs_runtime._semantic_slurm_evaluation import SemanticSlurmEvaluationExecutor
    from vs_runtime._step_runner import CancelSignal
    from vs_sim.api import BlockingRunner
    from vs_slurm.api import EvaluationLedger

_BACKEND = "slurm"


class SlurmJobStepRunner:
    """Runs ``SubmitMeasurement`` steps on the Slurm executor, which submits each job once."""

    def __init__(
        self,
        executor: SemanticSlurmEvaluationExecutor,
        ledger: EvaluationLedger,
        blocking: BlockingRunner,
        job: JobStepRun,
    ) -> None:
        """Run on *executor*, which attaches through *ledger*; *job* follows each job to its end.

        *blocking* carries the ledger's blocking reads off the event loop. *job* runs on the
        same *executor*.
        """
        self._executor = executor
        self._ledger = ledger
        self._blocking = blocking
        self._job = job

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

        The ledger decides, because it alone proves whether a job was submitted, also in the
        window where the executor has not yet saved the job's handle. ``Adopted(None)`` for a
        request that is claimed, queued, running or ended: the next ``run`` attaches, and for
        an ended job returns its collected result without new work. A request the ledger holds
        withdrawn, or not at all, left no job to take over, and a step that cannot run, or
        that only reads another's result, left nothing. A result is not built here: the
        step's observation comes from the job's own end. A ledger that cannot be read raises
        ``ClearUnavailableError``: neither a job nor its absence is proven.
        """
        submission = submission_of(step)
        if submission.reuses is not None:
            return Cleared()
        try:
            key = await self._key_of(submission, tag)
            entry = await self._blocking.run(self._ledger.read, key)
        except (RejectedPlanError, ExecutorRejectedError):
            return Cleared()
        except LedgerError as error:
            raise ClearUnavailableError(tag, _BACKEND) from error
        if entry is None or entry.record.state is LedgerState.WITHDRAWN:
            return Cleared()
        return Adopted(None)

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
