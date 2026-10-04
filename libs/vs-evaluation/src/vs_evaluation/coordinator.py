"""Durable idempotent submission, observation, cancellation, and bounded waiting."""

from __future__ import annotations

import asyncio
import hashlib
import math
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from vs_evaluation.models import (
    EvaluationAwaitResult,
    EvaluationCanceled,
    EvaluationCompleted,
    EvaluationFailed,
    EvaluationLifecycleEvent,
    EvaluationLifecyclePhase,
    EvaluationRequest,
    EvaluationState,
    EvaluationStatus,
    EvaluationTimedOut,
    ExecutorObservation,
    StageState,
    StoredEvaluation,
)
from vs_evaluation.ports import (
    NULL_EVALUATION_EVENT_SINK,
    ExecutorRejectedError,
    ExecutorSubmissionError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_evaluation.models import AvailabilitySnapshot, ResourceRequirements
    from vs_evaluation.ports import (
        Clock,
        DeadlineScope,
        EvaluationEventSink,
        EvaluationExecutor,
        EvaluationStore,
    )

_TERMINAL = frozenset(
    {
        EvaluationState.SUCCEEDED,
        EvaluationState.FAILED,
        EvaluationState.CANCELED,
        EvaluationState.SUPERSEDED,
    }
)
_STATE_ORDER = {
    EvaluationState.QUEUED: 0,
    EvaluationState.STARTING: 1,
    EvaluationState.RUNNING: 2,
    EvaluationState.SUCCEEDED: 3,
    EvaluationState.FAILED: 3,
    EvaluationState.CANCELED: 3,
    EvaluationState.SUPERSEDED: 3,
}


class EvaluationLifecycleError(RuntimeError):
    """The persisted or executor-reported lifecycle violates its contract."""

    def __init__(self, code: LifecycleErrorCode, detail: str | None = None) -> None:
        """Build a stable diagnostic from a closed reason code."""
        messages = {
            LifecycleErrorCode.AVAILABILITY_FROM_FUTURE: "availability observation is from the future",
            LifecycleErrorCode.TERMINAL_TRANSITION: "terminal state changed",
            LifecycleErrorCode.STATE_REGRESSION: "lifecycle state regressed",
            LifecycleErrorCode.UNKNOWN_HANDLE: "unknown evaluation handle",
            LifecycleErrorCode.UNKNOWN_STAGE: "executor reported an unknown stage",
            LifecycleErrorCode.DUPLICATE_STAGE_RESULTS: "executor reported duplicate stage results",
            LifecycleErrorCode.STAGE_RESULT_ORDER: "executor reported stage results out of order",
            LifecycleErrorCode.STOP_ON_FAILURE: "later stage must be skipped after failure",
            LifecycleErrorCode.INCOMPLETE_SUCCESS: "successful evaluation omitted planned stages",
            LifecycleErrorCode.UNSUCCESSFUL_STAGE: "successful evaluation has a non-success stage",
        }
        message = messages[code]
        super().__init__(f"{message}: {detail}" if detail is not None else message)


class LifecycleErrorCode(StrEnum):
    """Closed diagnostic vocabulary for invalid lifecycle transitions."""

    AVAILABILITY_FROM_FUTURE = "availability_from_future"
    TERMINAL_TRANSITION = "terminal_transition"
    STATE_REGRESSION = "state_regression"
    UNKNOWN_HANDLE = "unknown_handle"
    UNKNOWN_STAGE = "unknown_stage"
    DUPLICATE_STAGE_RESULTS = "duplicate_stage_results"
    STAGE_RESULT_ORDER = "stage_result_order"
    STOP_ON_FAILURE = "stop_on_failure"
    INCOMPLETE_SUCCESS = "incomplete_success"
    UNSUCCESSFUL_STAGE = "unsuccessful_stage"


class EvaluationTimeoutError(ValueError):
    """A caller wait or configured coordinator maximum is invalid."""

    def __init__(self, *, maximum: float | None = None) -> None:
        """Report a positive finite timeout or over-limit request."""
        if maximum is None:
            super().__init__("timeout must be finite and positive")
        else:
            super().__init__(f"timeout_s exceeds coordinator maximum {maximum:g}")


class EvaluationKeyConflictError(ValueError):
    """A caller reused an idempotency key for a different request."""

    def __init__(self, key: str) -> None:
        """Name the offending idempotency key."""
        super().__init__(f"evaluation key {key!r} already identifies a different request")
        self.key = key


class RevisionConflictError(EvaluationLifecycleError):
    """A compare-and-set observed a newer durable revision."""

    def __init__(self, handle_id: str, actual: int, expected: int) -> None:
        """Name the record and competing revisions."""
        super().__init__(
            LifecycleErrorCode.STATE_REGRESSION,
            f"{handle_id!r} revision is {actual}; expected {expected}",
        )


class EvaluationCoordinator:
    """Coordinate durable idempotency and lifecycle around injected ports.

    The store is authoritative across restarts. The executor must make
    submission idempotent for a handle ID and expose inspect/wait/cancel by
    that same ID. Submit errors leave the record reconcilable because remote
    acceptance may have happened before the client observed the error.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-040302 [PLR0913]; executor, store, clock, wait policy, deadline scope, and event sink are independent injected coordinator ports.
        # > A settings aggregate would mix policy with effects; a setter would allow missing early events.
        self,
        executor: EvaluationExecutor,
        store: EvaluationStore,
        clock: Clock,
        *,
        max_await_timeout_s: float = 300.0,
        deadline_factory: Callable[[float], DeadlineScope] | None = None,
        events: EvaluationEventSink = NULL_EVALUATION_EVENT_SINK,
    ) -> None:
        """Bind ports and the largest permitted caller wait."""
        if not math.isfinite(max_await_timeout_s) or max_await_timeout_s <= 0:
            raise EvaluationTimeoutError
        self._executor = executor
        self._store = store
        self._clock = clock
        self._max_await_timeout_s = max_await_timeout_s
        self._events = events
        # asyncio.Timeout provides this structural scope at runtime; ty does
        # not recognize its context-manager signature as the port protocol.
        self._deadline_factory = deadline_factory or cast(
            "Callable[[float], DeadlineScope]", asyncio.timeout
        )
        self._locks: dict[str, asyncio.Lock] = {}

    async def status(self, handle_id: str) -> EvaluationStatus:
        """Refresh an evaluation and return its current lifecycle state."""
        return (await self._refresh(handle_id)).status

    async def recorded_status(self, handle_id: str) -> EvaluationStatus:
        """Read durable status without dispatching or inspecting external work."""
        return (await self._required_record(handle_id)).status

    async def snapshot(self, handle_id: str) -> StoredEvaluation:
        """Refresh an evaluation and return its complete durable record."""
        return await self._refresh(handle_id)

    async def cancel(self, handle_id: str) -> StoredEvaluation:
        """Request cancellation and return its latest observed record."""
        return await self._cancel(handle_id)

    async def await_result(self, handle_id: str, timeout_s: float) -> EvaluationAwaitResult:
        """Wait up to the required bounded timeout for one evaluation."""
        return await self._await_result(handle_id, timeout_s)

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        """Return the executor's typed, timestamped resource observation."""
        snapshot = await self._executor.availability(requirements)
        if snapshot.observed_at > self._clock.monotonic():
            raise EvaluationLifecycleError(LifecycleErrorCode.AVAILABILITY_FROM_FUTURE)
        return snapshot

    async def history(self) -> tuple[StoredEvaluation, ...]:
        """Return durable lifecycle history without inspecting or submitting work."""
        return await self._store.records()

    async def prepare(self, request: EvaluationRequest) -> EvaluationHandle:
        """Commit a stable request and its owner without dispatching external work."""
        handle_id = stable_handle_id(request.key)
        record = await self._store.claim(request, handle_id=handle_id)
        if record.handle_id != handle_id or record.request != request:
            raise EvaluationKeyConflictError(request.key)
        self._publish_record(record)
        return EvaluationHandle(self, handle_id)

    async def submit(self, request: EvaluationRequest) -> EvaluationHandle:
        """Durably claim work and return its stable handle without awaiting completion."""
        handle = await self.prepare(request)
        record = await self._required_record(handle.id)
        if record.state not in _TERMINAL:
            await self._ensure_submitted(record)
        return handle

    async def reconcile(self) -> tuple[EvaluationHandle, ...]:
        """Restore accepted/in-flight records after restart using stable IDs."""
        handles: list[EvaluationHandle] = []
        for record in await self._store.nonterminal():
            handle = EvaluationHandle(self, record.handle_id)
            await self._ensure_submitted(record)
            handles.append(handle)
        return tuple(handles)

    async def _ensure_submitted(self, record: StoredEvaluation) -> StoredEvaluation:
        async with self._lock_for(record.handle_id):
            current = await self._required_record(record.handle_id)
            if current.state in _TERMINAL:
                return current
            if current.cancel_requested:
                await self._executor.cancel(current.handle_id)
                observed = await self._executor.inspect(current.handle_id)
                if observed is None:
                    return current
                return await self._apply_observation(current, observed)
            observed = await self._executor.inspect(current.handle_id)
            # The durable pending bit is authoritative.  An executor may expose
            # a provisional QUEUED observation before provider acceptance, and
            # that local observation can outlive the client task that initiated
            # submission.  Retry with the same stable handle so a later client
            # session cannot strand the durable operation.  Executor submission
            # is idempotent by contract.
            if current.submission_pending or observed is None:
                current, sent = await self._send_submission(current)
                if not sent:
                    return current
                observed = await self._executor.inspect(current.handle_id)
            if observed is None:
                return current
            return await self._apply_observation(current, observed)

    async def _send_submission(self, current: StoredEvaluation) -> tuple[StoredEvaluation, bool]:
        """Submit once; return the latest record and whether the executor took the request."""
        if current.dispatch_authorized is not True:
            authorized = current.model_copy(
                update={"dispatch_authorized": True, "revision": current.revision + 1}
            )
            try:
                current = await self._store.compare_and_set(
                    authorized, expected_revision=current.revision
                )
            except RevisionConflictError:
                current = await self._required_record(current.handle_id)
                if (
                    current.state in _TERMINAL
                    or current.cancel_requested
                    or current.dispatch_authorized is not True
                ):
                    return current, False
            self._publish_record(current)
        try:
            await self._executor.submit(current.request, handle_id=current.handle_id)
        except ExecutorSubmissionError:
            # The provider may have accepted before a transport error.
            # Preserve the same idempotency key for later reconciliation.
            return current, False
        except ExecutorRejectedError as rejection:
            return await self._mark_rejected(current, str(rejection)), False
        return await self._mark_submission_sent(current), True

    async def _mark_rejected(self, current: StoredEvaluation, reason: str) -> StoredEvaluation:
        """Fail a handle the executor refused, so no caller waits on it forever."""
        updated = current.model_copy(
            update={
                "state": EvaluationState.FAILED,
                "failure": f"executor rejected the evaluation: {reason}",
                "submission_pending": False,
                "revision": current.revision + 1,
            }
        )
        try:
            stored = await self._store.compare_and_set(updated, expected_revision=current.revision)
        except RevisionConflictError:
            return await self._required_record(current.handle_id)
        self._publish_record(stored)
        return stored

    async def _mark_submission_sent(self, current: StoredEvaluation) -> StoredEvaluation:
        if not current.submission_pending:
            return current
        updated = current.model_copy(
            update={
                "submission_pending": False,
                "revision": current.revision + 1,
            }
        )
        try:
            stored = await self._store.compare_and_set(updated, expected_revision=current.revision)
        except RevisionConflictError:
            return await self._required_record(current.handle_id)
        self._publish_record(stored)
        return stored

    async def _refresh(self, handle_id: str) -> StoredEvaluation:
        current = await self._required_record(handle_id)
        if current.state in _TERMINAL:
            return current
        if current.cancel_requested:
            async with self._lock_for(handle_id):
                current = await self._required_record(handle_id)
                if current.state in _TERMINAL:
                    return current
                await self._executor.cancel(handle_id)
                observed = await self._executor.inspect(handle_id)
                if observed is None:
                    return current
                return await self._apply_observation(current, observed)
        observed = await self._executor.inspect(handle_id)
        if observed is None or current.submission_pending:
            return await self._ensure_submitted(current)
        return await self._apply_observation(current, observed)

    async def _apply_observation(
        self, current: StoredEvaluation, observed: ExecutorObservation
    ) -> StoredEvaluation:
        if current.state in _TERMINAL:
            if current.state is not observed.state:
                raise EvaluationLifecycleError(
                    LifecycleErrorCode.TERMINAL_TRANSITION,
                    f"{current.state.value!r} -> {observed.state.value!r}",
                )
            return current
        if _STATE_ORDER[observed.state] < _STATE_ORDER[current.state]:
            raise EvaluationLifecycleError(
                LifecycleErrorCode.STATE_REGRESSION,
                f"{current.state.value!r} -> {observed.state.value!r}",
            )
        _validate_stage_observation(current, observed)
        if (
            current.state is observed.state
            and current.current_stage == observed.current_stage
            and current.stage_results == observed.stage_results
            and current.failure == observed.failure
            and not current.submission_pending
        ):
            return current
        updated = current.model_copy(
            update={
                "state": observed.state,
                "current_stage": observed.current_stage,
                "stage_results": observed.stage_results,
                "failure": observed.failure,
                "submission_pending": False,
                "revision": current.revision + 1,
            }
        )
        try:
            stored = await self._store.compare_and_set(updated, expected_revision=current.revision)
        except RevisionConflictError:
            return await self._required_record(current.handle_id)
        self._publish_record(stored)
        return stored

    async def _cancel(self, handle_id: str) -> StoredEvaluation:
        async with self._lock_for(handle_id):
            current = await self._required_record(handle_id)
            if current.state in _TERMINAL:
                return current
            if current.dispatch_authorized is False and current.submission_pending:
                canceled = current.model_copy(
                    update={
                        "state": EvaluationState.CANCELED,
                        "cancel_requested": True,
                        "submission_pending": False,
                        "revision": current.revision + 1,
                    }
                )
                try:
                    stored = await self._store.compare_and_set(
                        canceled, expected_revision=current.revision
                    )
                except RevisionConflictError:
                    current = await self._required_record(current.handle_id)
                    if current.state in _TERMINAL:
                        return current
                else:
                    self._publish_record(stored)
                    return stored
            if not current.cancel_requested:
                requested = current.model_copy(
                    update={
                        "cancel_requested": True,
                        "revision": current.revision + 1,
                    }
                )
                try:
                    current = await self._store.compare_and_set(
                        requested, expected_revision=current.revision
                    )
                    self._publish_record(current)
                except RevisionConflictError:
                    current = await self._required_record(handle_id)
        return await self._refresh(handle_id)

    async def _await_result(self, handle_id: str, timeout_s: float) -> EvaluationAwaitResult:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise EvaluationTimeoutError
        if timeout_s > self._max_await_timeout_s:
            raise EvaluationTimeoutError(maximum=self._max_await_timeout_s)
        deadline = self._clock.monotonic() + timeout_s
        last_status: EvaluationStatus | None = None
        last_revision: int | None = None
        hard_deadline = self._deadline_factory(timeout_s)
        try:
            async with hard_deadline:
                # Read the authoritative status before an executor inspection
                # that may consume the remainder of the caller's budget.
                initial = await self._required_record(handle_id)
                last_status = initial.status
                last_revision = initial.revision
                while True:
                    record = await self._refresh(handle_id)
                    last_status = record.status
                    last_revision = record.revision
                    # A finished record is the answer even when reading it
                    # used up the caller's wait.
                    terminal = self._terminal_result(record)
                    if terminal is not None:
                        return terminal
                    remaining = deadline - self._clock.monotonic()
                    if remaining <= 0:
                        result = EvaluationTimedOut(handle_id=handle_id, status=last_status)
                        self._publish_timeout(handle_id, last_status, last_revision)
                        return result
                    await self._executor.wait_for_change(handle_id, remaining)
        except TimeoutError:
            if not hard_deadline.expired():
                raise
            # An inspect or store read may still be pending when the caller's
            # deadline expires. No truthful state exists until the first read.
            result = EvaluationTimedOut(handle_id=handle_id, status=last_status)
            self._publish_timeout(handle_id, last_status, last_revision)
            return result

    def _publish_record(self, record: StoredEvaluation) -> None:
        """Publish one durable snapshot through the injected observation port."""
        self._events(
            EvaluationLifecycleEvent(
                phase=_phase_for(record),
                handle_id=record.handle_id,
                revision=record.revision,
                state=record.state,
                current_stage=record.current_stage,
                stage_results=record.stage_results,
                failure=record.failure,
                submission_pending=record.submission_pending,
                cancel_requested=record.cancel_requested,
            )
        )

    def _publish_timeout(
        self,
        handle_id: str,
        status: EvaluationStatus | None,
        revision: int | None,
    ) -> None:
        """Publish a bounded-wait outcome without changing durable state."""
        self._events(
            EvaluationLifecycleEvent(
                phase=EvaluationLifecyclePhase.TIMED_OUT,
                handle_id=handle_id,
                revision=revision,
                state=status,
            )
        )

    @staticmethod
    def _terminal_result(record: StoredEvaluation) -> EvaluationAwaitResult | None:
        if record.state is EvaluationState.SUCCEEDED:
            return EvaluationCompleted(handle_id=record.handle_id, stages=record.stage_results)
        if record.state is EvaluationState.FAILED:
            return EvaluationFailed(handle_id=record.handle_id, message=record.failure or "")
        if record.state in {EvaluationState.CANCELED, EvaluationState.SUPERSEDED}:
            return EvaluationCanceled(handle_id=record.handle_id, state=record.state)
        return None

    async def _required_record(self, handle_id: str) -> StoredEvaluation:
        record = await self._store.get(handle_id)
        if record is None:
            raise EvaluationLifecycleError(LifecycleErrorCode.UNKNOWN_HANDLE, handle_id)
        return record

    def _lock_for(self, handle_id: str) -> asyncio.Lock:
        lock = self._locks.get(handle_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[handle_id] = lock
        return lock


class EvaluationHandle:
    """Stable client handle; observations reload authoritative state from the store."""

    def __init__(self, coordinator: EvaluationCoordinator, handle_id: str) -> None:
        """Bind the opaque stable ID to its coordinator."""
        self._coordinator = coordinator
        self.id = handle_id

    async def status(self) -> EvaluationStatus:
        """Refresh executor state and return its current lifecycle state."""
        return await self._coordinator.status(self.id)

    async def snapshot(self) -> StoredEvaluation:
        """Refresh and return the complete authoritative lifecycle record."""
        return await self._coordinator.snapshot(self.id)

    async def cancel(self) -> StoredEvaluation:
        """Request cancellation and return the latest observed record."""
        return await self._coordinator.cancel(self.id)

    async def await_result(self, timeout_s: float) -> EvaluationAwaitResult:
        """Wait up to a required bounded timeout and return a typed outcome."""
        return await self._coordinator.await_result(self.id, timeout_s)


def stable_handle_id(key: str) -> str:
    """Return the stable, provider-independent handle ID for a caller key."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"eval_{digest}"


def _phase_for(record: StoredEvaluation) -> EvaluationLifecyclePhase:
    """Map one authoritative record to its semantic observation phase."""
    if record.submission_pending:
        return EvaluationLifecyclePhase.SUBMITTED
    return EvaluationLifecyclePhase(record.state.value)


def _validate_stage_observation(record: StoredEvaluation, observation: ExecutorObservation) -> None:
    """Enforce the caller's ordered stage plan at the executor boundary."""
    names = [step.name for step in record.request.stages]
    indices = {name: index for index, name in enumerate(names)}
    if observation.current_stage is not None and observation.current_stage not in indices:
        raise EvaluationLifecycleError(LifecycleErrorCode.UNKNOWN_STAGE, observation.current_stage)
    result_names = [stage.name for stage in observation.stage_results]
    if len(result_names) != len(set(result_names)):
        raise EvaluationLifecycleError(LifecycleErrorCode.DUPLICATE_STAGE_RESULTS)
    if any(name not in indices for name in result_names):
        unknown = next(name for name in result_names if name not in indices)
        raise EvaluationLifecycleError(LifecycleErrorCode.UNKNOWN_STAGE, unknown)
    _validate_result_order(result_names, indices)
    _validate_stop_on_failure(record, observation, indices)
    _validate_successful_stages(names, observation)


def _validate_result_order(result_names: list[str], indices: dict[str, int]) -> None:
    result_indices = [indices[name] for name in result_names]
    if result_indices != sorted(result_indices):
        raise EvaluationLifecycleError(LifecycleErrorCode.STAGE_RESULT_ORDER)


def _validate_stop_on_failure(
    record: StoredEvaluation,
    observation: ExecutorObservation,
    indices: dict[str, int],
) -> None:
    failed_indices = [
        index
        for index, stage in enumerate(observation.stage_results)
        if stage.state is StageState.FAILED
    ]
    if record.request.stop_on_failure and failed_indices:
        result_indices = [indices[stage.name] for stage in observation.stage_results]
        failed_index = result_indices[failed_indices[0]]
        for index, stage in zip(result_indices, observation.stage_results, strict=True):
            if index > failed_index and stage.state is not StageState.SKIPPED:
                raise EvaluationLifecycleError(LifecycleErrorCode.STOP_ON_FAILURE)


def _validate_successful_stages(names: list[str], observation: ExecutorObservation) -> None:
    if observation.state is not EvaluationState.SUCCEEDED:
        return
    result_names = [stage.name for stage in observation.stage_results]
    if result_names != names:
        raise EvaluationLifecycleError(LifecycleErrorCode.INCOMPLETE_SUCCESS)
    if any(stage.state is not StageState.SUCCEEDED for stage in observation.stage_results):
        raise EvaluationLifecycleError(LifecycleErrorCode.UNSUCCESSFUL_STAGE)


__all__ = [
    "EvaluationCoordinator",
    "EvaluationHandle",
    "EvaluationKeyConflictError",
    "EvaluationLifecycleError",
    "RevisionConflictError",
    "stable_handle_id",
]
