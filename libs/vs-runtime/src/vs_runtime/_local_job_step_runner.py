"""A step that is a local evaluation: a stage process on this host, started again from clean.

Nothing of a local evaluation survives its host by design, so a re-run never attaches: ``clear``
ends whatever an earlier attempt left and the next attempt runs under a handle of its own, so
the executor never answers it with the cancelled attempt's partial result. What an earlier
attempt left is the evaluation task of this process, which the executor cancels, and the
stage processes and containers a dead process left, which the host's ``StageWorkReaper``
finds by the step's tag.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vs_evaluation.api import EvaluationState
from vs_runtime._evaluation_jobs import handle_for
from vs_runtime._job_step_run import JobStepRun
from vs_runtime._step_runner import Cleared

if TYPE_CHECKING:
    from vs_core.api import Request, RequestId
    from vs_core.api.recovery import StepResult
    from vs_evaluation.api import PollingEvaluationExecutor
    from vs_runtime._step_runner import Adopted, CancelSignal
    from vs_sim.api import Clock


class StageWorkReaper(Protocol):
    """Ends the work of a step's evaluation stages that this process does not own."""

    async def reap(self, tag: RequestId) -> None:
        """Kill every process group of ``tag``'s stages, remove its containers, drop partial output.

        Returns only when nothing carrying the tag runs. Raises ``ClearUnavailableError`` when
        the process table or the container runtime cannot be reached.
        """
        ...


class LocalJobStepRunner:
    """Runs ``SubmitMeasurement`` steps on the in-process evaluation executor."""

    def __init__(
        self,
        executor: PollingEvaluationExecutor,
        reaper: StageWorkReaper,
        *,
        clock: Clock,
        poll_interval_s: float,
    ) -> None:
        """Run on *executor*; leave to *reaper* what a dead process left on the host."""
        self._executor = executor
        self._reaper = reaper
        self._job = JobStepRun(executor, clock=clock, poll_interval_s=poll_interval_s)
        self._attempts: dict[RequestId, list[str]] = {}

    async def run(
        self, step: Request, tag: RequestId, attempt: int, cancel: CancelSignal
    ) -> StepResult:
        """Run the step's evaluation as attempt ``attempt``, under a handle of its own."""
        handle = f"{handle_for(tag)}-attempt-{attempt}"
        self._attempts.setdefault(tag, []).append(handle)
        return await self._job.run(step, tag, handle, cancel)

    async def clear(self, tag: RequestId) -> Adopted | Cleared:
        """End every evaluation of ``tag`` this process runs, then what a dead process left."""
        for handle in self._attempts.get(tag, ()):
            observed = await self._executor.inspect_only(handle)
            if observed is not None and not _ended(observed.state):
                await self._executor.cancel(handle)
        await self._reaper.reap(tag)
        return Cleared()


def _ended(state: EvaluationState) -> bool:
    return state in {EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED}
