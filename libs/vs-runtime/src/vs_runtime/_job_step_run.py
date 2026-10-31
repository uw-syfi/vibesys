"""Run one measurement submission to its end on an evaluation executor, and say how it ended.

``SlurmJobStepRunner`` and ``LocalJobStepRunner`` share this: they differ in what ``clear``
removes or adopts, not in how a submission runs. A run is idempotent in the executor's own
terms: the handle is a pure function of the step's tag, and an executor that already holds
the handle attaches to its work instead of starting more (the Slurm executor through its
ledger key, the local one through its task table). So a second run of one tag never starts
a second copy of the job, and a run that returns has left nothing running.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from vs_core.api import (
    ContractError,
    EventId,
    MeasurementPlan,
    Observation,
    SubmitMeasurement,
)
from vs_core.api.recovery import Cancelled, Finished
from vs_evaluation.api import EvaluationState, ExecutorRejectedError, PollPhase
from vs_runtime._core_identity import core_named
from vs_runtime._evaluation_jobs import (
    RejectedPlanError,
    job_view,
    measurement_request,
)
from vs_runtime._observation_factory import ObservationFacts, ObservationSubject

if TYPE_CHECKING:
    from vs_core.api import Request, RequestId
    from vs_core.api.recovery import StepResult
    from vs_evaluation.api import ExecutorPoll, PollingEvaluationExecutor
    from vs_runtime._step_runner import CancelSignal
    from vs_sim.api import Clock

_UNREADABLE_RUN = "the run this measurement reuses can no longer be read as ended"


class StillRunningAfterCancelError(RuntimeError):
    """A cancelled job was not seen to end, so the run has no result to return.

    The run raises it instead of answering ``Cancelled`` while the job may still run. The
    loop then treats the attempt as interrupted and calls ``clear``, which finds the job.
    """

    def __init__(self, handle_id: str) -> None:
        """Name the handle that is still running."""
        super().__init__(f"evaluation {handle_id!r} was cancelled but is not seen to have ended")


class JobStepRun:
    """Submits a step's evaluation once and follows it to an end, or to a cancel."""

    def __init__(
        self, executor: PollingEvaluationExecutor, *, clock: Clock, poll_interval_s: float
    ) -> None:
        """Run on *executor*; stamp observations with *clock*; re-poll at least every interval."""
        if poll_interval_s <= 0:
            message = f"poll_interval_s must be positive, got {poll_interval_s}"
            raise ValueError(message)
        self._executor = executor
        self._clock = clock
        self._poll_interval_s = poll_interval_s

    async def run(
        self, step: Request, tag: RequestId, handle: str, cancel: CancelSignal
    ) -> StepResult:
        """Return the result of the submission ``step``, run under ``handle``, or ``Cancelled``.

        The executor attaches to the job of a handle it already holds, so one handle is one job.
        """
        submission = _submission(step)
        try:
            request = measurement_request(
                submission.plan, submission.scope, handle, deadline_at=_deadline(submission.plan)
            )
            await self._executor.submit(request, handle_id=handle)
        except (RejectedPlanError, ExecutorRejectedError) as error:
            return self._refused(submission, tag, str(error))
        while True:
            polled = await self._executor.poll(handle)
            if polled.phase is PollPhase.ENDED:
                return self._finished(polled, submission, tag, handle)
            if cancel.is_set():
                return await self._cancelled(submission, tag, handle, cancel)
            await self._until_change_or_cancel(handle, cancel)

    async def read_ended(self, step: Request, tag: RequestId, source: str) -> StepResult:
        """The result of ``step`` read from the ended job of ``source``, which it reuses.

        A job that is not seen ended is a refusal, so core measures afresh.
        """
        submission = _submission(step)
        polled = await self._executor.poll(source)
        if polled.phase is PollPhase.ENDED:
            return self._finished(polled, submission, tag, source)
        return self._refused(submission, tag, _UNREADABLE_RUN)

    async def _until_change_or_cancel(self, handle: str, cancel: CancelSignal) -> None:
        changed = asyncio.ensure_future(self._executor.wait_for_change(handle, self._poll_interval_s))
        cancelled = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({changed, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiting in (changed, cancelled):
                waiting.cancel()
            await asyncio.gather(changed, cancelled, return_exceptions=True)

    async def _cancelled(
        self, step: SubmitMeasurement, tag: RequestId, handle: str, cancel: CancelSignal
    ) -> StepResult:
        """Cancel the job, and answer ``Cancelled`` only once it is seen ended."""
        reason = cancel.reason
        if reason is None:
            message = "a job is cancelled only for a reason"
            raise ContractError(("cancel", "reason"), message)
        await self._executor.cancel(handle)
        polled = await self._executor.poll(handle)
        if polled.phase is not PollPhase.ENDED:
            raise StillRunningAfterCancelError(handle)
        if polled.terminal is not None and polled.terminal.state is not EvaluationState.CANCELED:
            return self._finished(polled, step, tag, handle)
        return Cancelled(reason=reason)

    def _finished(
        self, polled: ExecutorPoll, step: SubmitMeasurement, tag: RequestId, handle: str
    ) -> StepResult:
        subject = ObservationSubject(core_named(tag), step.scope, step.admission_id)
        view = job_view(
            polled,
            step.plan,
            subject,
            handle,
            self._clock.now(),
            lambda facts: self._observation(subject, facts),
        )
        return Finished(observation=view.observation)

    def _refused(self, step: SubmitMeasurement, tag: RequestId, reason: str) -> StepResult:
        subject = ObservationSubject(core_named(tag), step.scope, step.admission_id)
        return Finished(observation=self._observation(subject, ObservationFacts.refused(reason)))

    def _observation(self, subject: ObservationSubject, facts: ObservationFacts) -> Observation:
        """The one observation a step reports: a step ends once, so its sequence is 0."""
        return Observation(
            event_id=EventId(root=f"{subject.request_id.root}:observation:0"),
            request_id=subject.request_id,
            scope=subject.scope,
            admission_id=subject.admission_id,
            sequence=0,
            observed_at=self._clock.now(),
            status=facts.status,
            resource_id=facts.resource_id,
            accepted=facts.accepted,
            terminal=facts.terminal,
            released=facts.released,
            children=facts.children,
            children_complete=facts.children_complete,
            diagnostic=facts.diagnostic,
        )


def _submission(step: Request) -> SubmitMeasurement:
    if not isinstance(step, SubmitMeasurement):
        message = f"a job step runner runs submit_measurement, not {step.kind}"
        raise ContractError(("step", "kind"), message)
    return step


def _deadline(plan: MeasurementPlan) -> float:
    return plan.submitted_at + plan.time_bound
