"""Use stronger owner knowledge whenever a native delivery may have escaped."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import CancelOwnedJob, ObservationStatus, ObserveOwnedJob, SubmitMeasurement
from vs_evaluation.api import ExecutorCancellationUnknownError, PollPhase
from vs_runtime._core_requests import ExecutorRefusal, ExecutorRole
from vs_runtime._evaluation_jobs import handle_for
from vs_runtime._evaluation_requests import MeasurementRequests
from vs_runtime._receipt_store import NeverBegun

if TYPE_CHECKING:
    from vs_evaluation.api import (
        AvailabilitySnapshot,
        EvaluationRequest,
        ExecutorObservation,
        ExecutorPoll,
        RecoverableEvaluationExecutor,
        ResourceRequirements,
    )
    from vs_runtime._core_requests import (
        EvaluationRoleRequest,
        ExecutionContext,
        ExecutionOutcome,
        ExecutionResult,
    )
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._replay_operations import ReplayOperationHost
    from vs_runtime._run_control import RunControlChannel


class ReplayEvaluationJobs:
    """Adapt fresh dispatch and authoritative recovery to the existing measurement owner.

    Fresh handles are authorized only while the replay receipt proves NeverBegun.
    Every other poll asks the stronger recovery interface. Missing process-local
    work remains unknown, including when a close tries to infer release.
    """

    def __init__(self, inner: RecoverableEvaluationExecutor) -> None:
        self._inner = inner
        self.fresh: set[str] = set()

    async def poll(self, handle_id: str) -> ExecutorPoll:
        return await (
            self._inner.poll(handle_id)
            if handle_id in self.fresh
            else self._inner.recover(handle_id)
        )

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        await self._inner.submit(request, handle_id=handle_id)

    async def cancel(self, handle_id: str) -> None:
        knowledge = await self.poll(handle_id)
        if knowledge.phase is PollPhase.UNKNOWN:
            problem = "cannot confirm cancellation of an unknown earlier evaluation"
            raise ExecutorCancellationUnknownError(problem)
        await self._inner.cancel(handle_id)

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        await self._inner.wait_for_change(handle_id, timeout_s)

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        return await self._inner.inspect(handle_id)

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        return await self._inner.inspect_only(handle_id)

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        return await self._inner.availability(requirements)


class RecoveringMeasurementRequests:
    """Preserve the old measurement owner while separating fresh work from recovery."""

    def __init__(
        self,
        jobs: RecoverableEvaluationExecutor,
        receipts: ReceiptStore,
        host: ReplayOperationHost,
        control: RunControlChannel | None,
    ) -> None:
        self._jobs, self._receipts, self._host = jobs, receipts, host
        self._physical = ReplayEvaluationJobs(jobs)
        self._owner = MeasurementRequests(self._physical, receipts, control=control)

    async def _perform(
        self, request: EvaluationRoleRequest, context: ExecutionContext
    ) -> ExecutionResult:
        if (
            isinstance(request, ObserveOwnedJob)
            and (await self._jobs.recover(request.resource_id.root)).phase is not PollPhase.UNKNOWN
        ):
            await self._jobs.wait_for_change(
                request.resource_id.root, min(1, max(0, context.until.at - self._host.clock.now()))
            )
        result = await self._owner.execute(request, context)
        seen = result.observation.observation
        awaiting_acceptance = (
            isinstance(request, SubmitMeasurement) and seen.status is ObservationStatus.UNKNOWN
        )
        awaiting_release = isinstance(request, CancelOwnedJob) and not seen.released
        if (
            awaiting_release
            and isinstance(request, CancelOwnedJob)
            and (await self._jobs.recover(request.resource_id.root)).phase is PollPhase.UNKNOWN
        ):
            return result
        if awaiting_acceptance or awaiting_release:
            handles = (
                (handle_for(request.request_id),)
                if isinstance(request, SubmitMeasurement) and request.request_id is not None
                else tuple(item.root for item in seen.children)
            )
            if isinstance(request, CancelOwnedJob):
                handles = (request.resource_id.root,)
            remaining = max(0.0, context.until.at - self._host.clock.now())
            for handle in handles:
                await self._jobs.wait_for_change(handle, remaining / max(1, len(handles)))
            result = await self._owner.execute(
                request, context.model_copy(update={"now_at": self._host.clock.now()})
            )
        return result

    async def execute(
        self, request: EvaluationRoleRequest, context: ExecutionContext
    ) -> ExecutionOutcome:
        fresh = (
            isinstance(request, SubmitMeasurement)
            and request.request_id is not None
            and isinstance(self._receipts.history(request.request_id.root), NeverBegun)
        )
        handle = (
            handle_for(request.request_id) if fresh and request.request_id is not None else None
        )
        if isinstance(request, SubmitMeasurement) and not fresh and request.request_id is not None:
            knowledge = await self._jobs.recover(handle_for(request.request_id))
            if knowledge.phase is PollPhase.UNKNOWN:
                return ExecutorRefusal(
                    request_id=request.request_id,
                    role=ExecutorRole.EVALUATION,
                    detail=knowledge.detail
                    or "the earlier evaluation has no authoritative recovery result",
                )
        if handle is not None:
            self._physical.fresh.add(handle)
        try:
            return await self._perform(request, context)
        finally:
            if handle is not None:
                self._physical.fresh.discard(handle)
