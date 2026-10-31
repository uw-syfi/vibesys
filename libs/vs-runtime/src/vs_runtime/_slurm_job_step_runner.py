"""A step that is a Slurm evaluation job, run again by finding the job it already started.

The Slurm job outlives the host by design: the evaluation ledger on the cluster names it by
the request's idempotency key (``vs_slurm.ledger``), and binds each handle to that key. A later
process asks for the same handle, so the executor attaches to the job the earlier attempt
submitted instead of submitting again. ``clear`` therefore removes nothing: it asks the
ledger whether an earlier attempt left a request, and says so.

A job this runner cancelled is never attached to. The cancel interrupted the attempt (a pause,
a drain deadline) and left no result, so the next attempt has to run the step: it takes the
next handle of the tag, and the ledger, which holds a withdrawn attempt final, claims a new job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import SubmitMeasurement
from vs_evaluation.api import EvaluationState, PollPhase
from vs_runtime._evaluation_jobs import handle_for
from vs_runtime._job_step_run import JobStepRun
from vs_runtime._step_runner import Adopted, Cleared, ClearUnavailableError
from vs_slurm.api import LedgerError

if TYPE_CHECKING:
    from vs_core.api import Request, RequestId
    from vs_core.api.recovery import StepResult
    from vs_evaluation.api import ExecutorPoll, PollingEvaluationExecutor
    from vs_runtime._step_runner import CancelSignal
    from vs_sandbox.api.slurm import SlurmLedgerAttachment
    from vs_sim.api import BlockingRunner, Clock

_BACKEND = "slurm"


class SlurmJobStepRunner:
    """Runs ``SubmitMeasurement`` steps on the Slurm executor, which submits each job once."""

    def __init__(
        self,
        executor: PollingEvaluationExecutor,
        ledger: SlurmLedgerAttachment,
        blocking: BlockingRunner,
        *,
        clock: Clock,
        poll_interval_s: float,
    ) -> None:
        """Run on *executor*, which attaches through *ledger*; poll its jobs every interval.

        *blocking* carries the ledger's blocking reads off the event loop.
        """
        self._executor = executor
        self._ledger = ledger
        self._blocking = blocking
        self._job = JobStepRun(executor, clock=clock, poll_interval_s=poll_interval_s)

    async def run(
        self, step: Request, tag: RequestId, attempt: int, cancel: CancelSignal
    ) -> StepResult:
        """Run the step's job, or attach to the one an earlier attempt submitted, and wait.

        A step that reuses an earlier submission launches nothing and reads that job's result.
        """
        del attempt  # the job is found by the tag, so no attempt number names it
        if isinstance(step, SubmitMeasurement) and step.reuses is not None:
            source, _ = await self._current(step.reuses)
            return await self._job.read_ended(step, tag, source)
        handle, _ = await self._current(tag)
        return await self._job.run(step, tag, handle, cancel)

    async def clear(self, tag: RequestId) -> Adopted | Cleared:
        """Adopt the job an earlier attempt left, or report that it left none.

        The ledger decides, because it alone proves whether a job was submitted, also in the
        window where the executor has not yet saved the job's handle. ``Adopted(None)`` for a
        request that is claimed, queued, running or ended: the next ``run`` attaches, and for
        an ended job returns its collected result without new work. A result is not built
        here, because an observation needs the step's scope and plan and ``clear`` is given
        only the tag. A ledger that cannot be read raises ``ClearUnavailableError``: neither a
        job nor its absence is proven.
        """
        handle, _ = await self._current(tag)
        try:
            entry = await self._blocking.run(self._ledger.entry_of, handle)
        except LedgerError as error:
            raise ClearUnavailableError(tag, _BACKEND) from error
        return Cleared() if entry is None else Adopted(None)

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
