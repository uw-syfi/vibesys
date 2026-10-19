"""The backend's own retries of a measurement whose failure is not the candidate's.

A node fault, a lost job or an ambiguous failure says nothing about the candidate. The
backend measures such a candidate again, within the bounds it declares
(`EvaluationRetries`), and the search never sees the failed tries: it gets one result,
the last. A candidate's own failure is a result and is never measured again. Core
keeps only a hard cap (`MAX_SUBMISSIONS_PER_MEASUREMENT`) that no declaration can lift.

`RetryingEvaluationExecutor` wraps any `PollingEvaluationExecutor`. Try ``n`` of a
measurement runs under the inner handle `attempt_handle(handle_id, n)`; the wrapper
records the request and the current try durably (an `EvaluationStateNamespace`), so a
restarted host resumes the retrying instead of reporting the failure it was about to
retry.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vs_core.api import MAX_SUBMISSIONS_PER_MEASUREMENT, ContractValidationError, MeasurementFailure
from vs_evaluation.api import (
    EvaluationRequest,
    EvaluationState,
    EvidenceOutcome,
    ExecutorPoll,
    ExecutorRejectedError,
    PollingEvaluationExecutor,
    PollPhase,
    TrustedEvidence,
    same_work,
)
from vs_runtime._failure_classification import observed_failure

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from vs_evaluation.api import (
        AvailabilitySnapshot,
        EvaluationStateNamespace,
        ExecutorObservation,
        ResourceRequirements,
    )
    from vs_runtime._measurement_progress import StageOutputOpener, StageRun
    from vs_sim.api import OutputSink


class EvaluationRetries(BaseModel):
    """How often the backend measures one candidate in all, by why the earlier try failed.

    Both bounds count tries including the first, and neither may exceed core's hard cap.
    No default: the backend's configuration states them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    infrastructure_attempts: int = Field(ge=1, le=MAX_SUBMISSIONS_PER_MEASUREMENT)
    """Tries in all when a try fails for a reason in the machinery (node fault, lost job)."""
    ambiguous_attempts: int = Field(ge=1, le=MAX_SUBMISSIONS_PER_MEASUREMENT)
    """Tries in all when a try fails and the evidence cannot say whose fault it is."""

    @model_validator(mode="after")
    def ambiguity_is_not_retried_harder(self) -> EvaluationRetries:
        """Reject retrying an ambiguous failure more than a proven machinery failure."""
        if self.ambiguous_attempts > self.infrastructure_attempts:
            raise ContractValidationError(
                "ambiguous_attempts", "must not exceed infrastructure_attempts"
            )
        return self

    def allows_another(self, failure: MeasurementFailure | None, *, tries: int) -> bool:
        """Whether a measurement that failed as ``failure`` after ``tries`` tries is tried again."""
        if failure is MeasurementFailure.INFRASTRUCTURE:
            return tries < self.infrastructure_attempts
        if failure is MeasurementFailure.AMBIGUOUS:
            return tries < self.ambiguous_attempts
        return False


def terminal_failure(terminal: ExecutorObservation) -> MeasurementFailure | None:
    """Whose fault a terminal observation's failure is, or ``None`` when it did not fail."""
    if terminal.state is not EvaluationState.FAILED:
        return None
    return observed_failure(
        (
            evidence.failure_kind
            for step in terminal.stage_results
            if step.result is not None
            and (evidence := TrustedEvidence.model_validate(step.result)).outcome
            is EvidenceOutcome.FAILED
        ),
        run_environment_unavailable=terminal.run_environment_unavailable,
    )


def attempt_handle(handle_id: str, attempt: int) -> str:
    """The inner executor's handle for try number ``attempt`` (the first keeps the handle)."""
    return handle_id if attempt == 1 else f"{handle_id}-try-{attempt}"


class TryHandles:
    """Which try of which measurement each inner handle runs, as the wrapper submitted it.

    A ``RetryingEvaluationExecutor`` records every inner handle it submits here, so an
    inner executor's stage output can be named by the measurement's own handle (the one
    its progress reports name) and its try number. Share one with the wrapper and the
    inner executor's stage output opener; it is used on the executors' event loop only.
    """

    def __init__(self) -> None:
        """Know no tries yet."""
        self._tries: dict[str, tuple[str, int]] = {}

    def record(self, handle_id: str, attempt: int) -> None:
        """Remember that ``attempt_handle(handle_id, attempt)`` runs try ``attempt``."""
        self._tries[attempt_handle(handle_id, attempt)] = (handle_id, attempt)

    def stage_output(self, opener: StageOutputOpener) -> StageOutputOpener:
        """``opener``, called with the measurement's handle and try in place of the inner one.

        A stage of an inner handle this wrapper never submitted keeps its own handle and
        try 1: that executor runs it without retries.
        """

        def open_stage_output(run: StageRun) -> AbstractContextManager[OutputSink]:
            tried = self._tries.get(run.handle_id)
            if tried is None:
                return opener(run)
            handle_id, attempt = tried
            return opener(replace(run, handle_id=handle_id, attempt=attempt))

        return open_stage_output


class ClosableEvaluationExecutor(PollingEvaluationExecutor, Protocol):
    """A pollable executor the host releases at shutdown."""

    async def close(self) -> None:
        """Release the executor's workspaces and cancel its jobs."""
        ...


class _Tries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: EvaluationRequest
    attempt: int = Field(default=1, ge=1)


class RetryingEvaluationExecutor:
    """Measure again when a try fails for a reason that is not the candidate's."""

    def __init__(
        self,
        inner: ClosableEvaluationExecutor,
        retries: EvaluationRetries,
        namespace: EvaluationStateNamespace,
        *,
        tries: TryHandles | None = None,
    ) -> None:
        """Retry ``inner``'s infrastructure failures within ``retries``, recorded in ``namespace``.

        Each inner handle submitted is recorded in ``tries`` when one is given.
        """
        self._inner = inner
        self._tries = TryHandles() if tries is None else tries
        self._retries = retries
        self._namespace = namespace

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        """The inner executor's capacity."""
        return await self._inner.availability(requirements)

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        """Record the request, then start (or find) the current try."""
        tries = self._load(handle_id)
        if tries is None:
            tries = _Tries(request=request)
            self._namespace.save(self._path(handle_id), tries)
        elif not same_work(tries.request, request):
            message = f"retrying handle {handle_id!r} has different work"
            raise ExecutorRejectedError(message)
        elif tries.request != request:
            # The same work, recorded before its stages named their purpose: keep the
            # request as it is now asked for, so the tries to come carry the purpose.
            tries = tries.model_copy(update={"request": request})
            self._namespace.save(self._path(handle_id), tries)
        await self._submit_try(handle_id, tries)

    async def poll(self, handle_id: str) -> ExecutorPoll:
        """Inspect the current try; start the next one instead of reporting a retryable failure."""
        tries = self._load(handle_id)
        if tries is None:
            return await self._inner.poll(handle_id)
        current = attempt_handle(handle_id, tries.attempt)
        polled = await self._inner.poll(current)
        if polled.phase is PollPhase.UNSUBMITTED and tries.attempt > 1:
            # The next try was recorded and the host stopped before starting it.
            await self._submit_try(handle_id, tries)
            return ExecutorPoll(phase=PollPhase.QUEUED, attempt=polled.attempt)
        if polled.terminal is None:
            return polled
        failure = terminal_failure(polled.terminal)
        if not self._retries.allows_another(failure, tries=tries.attempt):
            return polled
        following = tries.model_copy(update={"attempt": tries.attempt + 1})
        self._namespace.save(self._path(handle_id), following)
        await self._submit_try(handle_id, following)
        return ExecutorPoll(phase=PollPhase.QUEUED, attempt=polled.attempt)

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        """The current try's observation, without dispatch."""
        return await self._inner.inspect_only(self._current(handle_id))

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        """The current try's observation."""
        return await self._inner.inspect(self._current(handle_id))

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        """Wait boundedly for the current try to change."""
        await self._inner.wait_for_change(self._current(handle_id), timeout_s)

    async def cancel(self, handle_id: str) -> None:
        """Cancel the current try; a cancelled try is never retried."""
        await self._inner.cancel(self._current(handle_id))

    async def close(self) -> None:
        """Close the inner executor."""
        await self._inner.close()

    async def _submit_try(self, handle_id: str, tries: _Tries) -> None:
        self._tries.record(handle_id, tries.attempt)
        await self._inner.submit(tries.request, handle_id=attempt_handle(handle_id, tries.attempt))

    def _current(self, handle_id: str) -> str:
        tries = self._load(handle_id)
        return handle_id if tries is None else attempt_handle(handle_id, tries.attempt)

    def _load(self, handle_id: str) -> _Tries | None:
        return self._namespace.load_optional(self._path(handle_id), _Tries)

    @staticmethod
    def _path(handle_id: str) -> str:
        return f"evaluation-tries/{handle_id}.json"


__all__ = [
    "ClosableEvaluationExecutor",
    "EvaluationRetries",
    "RetryingEvaluationExecutor",
    "TryHandles",
    "attempt_handle",
    "terminal_failure",
]
