"""A step that is a local evaluation: stages on this host, started again from clean.

Nothing of a local evaluation survives its host by design, so a re-run never attaches to an
earlier attempt's work. ``clear`` ends what an earlier attempt left and the next ``run`` starts
under a handle of its own, so the executor never answers it with the cancelled attempt's
partial result. Two things can be left:

- the evaluation task of this process, which the executor cancels and waits for;
- the stage processes and containers of a process that died, which a ``StageWorkReaper`` finds
  by the step's tag.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vs_core.api import SubmitMeasurement
from vs_evaluation.api import PollPhase
from vs_runtime._evaluation_jobs import handle_of_life
from vs_runtime._job_step_run import JobStepRun
from vs_runtime._marked_stage_work_reaper import MarkedStageWorkReaper
from vs_runtime._step_runner import Cleared

if TYPE_CHECKING:
    from vs_core.api import Request, RequestId
    from vs_core.api.recovery import StepResult
    from vs_evaluation.api import PollingEvaluationExecutor
    from vs_runtime._job_registry import JobRegistry
    from vs_runtime._step_runner import Adopted, CancelSignal
    from vs_sim.api import Clock, ProcessTable


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

    @classmethod
    def on_host(
        cls,
        executor: PollingEvaluationExecutor,
        processes: ProcessTable,
        registry: JobRegistry,
        *,
        clock: Clock,
        poll_interval_s: float,
    ) -> LocalJobStepRunner:
        """Run on *executor*, registering its jobs in *registry*.

        What a dead process left is found by the marks in the process table *processes*.
        """
        job = JobStepRun(executor, registry, clock=clock, poll_interval_s=poll_interval_s)
        return cls(executor, MarkedStageWorkReaper(processes), job)

    def __init__(
        self,
        executor: PollingEvaluationExecutor,
        reaper: StageWorkReaper,
        job: JobStepRun,
    ) -> None:
        """Run on *executor*; leave to *reaper* what a dead process left on the host.

        *job* follows each evaluation to its end, on the same *executor*.
        """
        self._executor = executor
        self._reaper = reaper
        self._job = job
        self._lives: dict[RequestId, int] = {}

    async def run(
        self,
        step: Request,
        tag: RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        """Run the step's evaluation from clean, or follow the one of this process still going.

        A step that reuses an earlier submission launches nothing and reads that job's result.
        """
        del attempt  # a job of this process is found by the tag and its life, never by attempt
        if isinstance(step, SubmitMeasurement) and step.reuses is not None:
            return await self._job.read_ended(step, tag, self._handle(step.reuses))
        if (await self._executor.poll(self._handle(tag))).phase is PollPhase.ENDED:
            self._lives[tag] = self._lives.get(tag, 0) + 1
        return await self._job.run(step, tag, self._handle(tag), deadline_at, cancel)

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """End every evaluation of ``tag`` this process runs, then what a dead process left."""
        del step  # the work of a local evaluation is found by the tag alone
        handle = self._handle(tag)
        polled = await self._executor.poll(handle)
        if polled.phase is not PollPhase.UNSUBMITTED:
            if polled.phase is not PollPhase.ENDED:
                await self._executor.cancel(handle)
            self._lives[tag] = self._lives.get(tag, 0) + 1
        await self._reaper.reap(tag)
        return Cleared()

    def _handle(self, tag: RequestId) -> str:
        """The executor handle of the tag's current life: each life is one run's evaluation."""
        return handle_of_life(tag, self._lives.get(tag, 0))
