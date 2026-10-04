"""In-memory implementations for lifecycle contract tests."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from vs_evaluation.coordinator import EvaluationKeyConflictError, RevisionConflictError
from vs_evaluation.models import (
    AvailabilitySnapshot,
    AvailabilityState,
    CostClass,
    EvaluationRequest,
    EvaluationState,
    EvaluationStepResult,
    ExecutorObservation,
    ResourceRequirements,
    ReuseStatus,
    StoredEvaluation,
)
from vs_evaluation.ports import ExecutorRejectedError, ExecutorSubmissionError
from vs_project.api import ProjectStateError, StateModelNotFoundError

if TYPE_CHECKING:
    from types import TracebackType


@dataclass
class FakeClock:
    """Manually advanced monotonic clock for deterministic timeout tests."""

    value: float = 0.0

    def monotonic(self) -> float:
        """Return the current fake time."""
        return self.value

    def advance(self, seconds: float) -> None:
        """Advance fake time without waiting on wall time."""
        if seconds < 0:
            raise ValueError("negative advance")  # noqa: TRY003  # lint-waiver: LW-930030 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        self.value += seconds


@dataclass
class FakeDeadlineScope:
    """Manually expire an active bounded operation without wall-clock waits."""

    requested_s: float
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[object] | None = None
    _expired: bool = False

    async def __aenter__(self) -> FakeDeadlineScope:
        """Capture the bounded task and announce entry to the test."""
        self._task = asyncio.current_task()
        self.entered.set()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Turn a manually expired cancellation into TimeoutError."""
        del exc_type, tb
        if self._expired and isinstance(exc, asyncio.CancelledError):
            if self._task is not None:
                self._task.uncancel()
            raise TimeoutError from exc
        return None

    def expired(self) -> bool:
        """Report whether the test fired this deadline."""
        return self._expired

    def expire(self) -> None:
        """Expire the active operation immediately."""
        if self._task is None:
            raise RuntimeError
        self._expired = True
        self._task.cancel()


@dataclass
class FakeDeadlineFactory:
    """Create inspectable manual deadline scopes for coordinator tests."""

    scopes: list[FakeDeadlineScope] = field(default_factory=list)

    def __call__(self, timeout_s: float) -> FakeDeadlineScope:
        """Retain one scope with the requested bound."""
        scope = FakeDeadlineScope(timeout_s)
        self.scopes.append(scope)
        return scope


class InMemoryEvaluationStore:
    """Faithful atomic in-memory implementation of EvaluationStore."""

    def __init__(self) -> None:
        """Create an empty store."""
        self._records: dict[str, StoredEvaluation] = {}
        self._handles_by_key: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def claim(self, request: EvaluationRequest, *, handle_id: str) -> StoredEvaluation:
        """Atomically claim an idempotency key or return its existing record."""
        async with self._lock:
            existing_id = self._handles_by_key.get(request.key)
            if existing_id is not None:
                existing = self._records[existing_id]
                if existing.request != request or existing.handle_id != handle_id:
                    raise EvaluationKeyConflictError(request.key)
                return existing
            record = StoredEvaluation(
                handle_id=handle_id,
                request=request,
                state=EvaluationState.QUEUED,
                revision=0,
                submission_pending=True,
                dispatch_authorized=False,
            )
            self._records[handle_id] = record
            self._handles_by_key[request.key] = handle_id
            return record

    async def get(self, handle_id: str) -> StoredEvaluation | None:
        """Return an immutable stored record."""
        async with self._lock:
            return self._records.get(handle_id)

    async def get_by_key(self, key: str) -> StoredEvaluation | None:
        """Return the immutable record associated with a caller key."""
        async with self._lock:
            handle_id = self._handles_by_key.get(key)
            return self._records.get(handle_id) if handle_id is not None else None

    async def compare_and_set(
        self, record: StoredEvaluation, *, expected_revision: int
    ) -> StoredEvaluation:
        """Atomically store a revision-checked record."""
        async with self._lock:
            current = self._records.get(record.handle_id)
            if current is None:
                raise KeyError(record.handle_id)
            if current.revision != expected_revision:
                raise RevisionConflictError(record.handle_id, current.revision, expected_revision)
            if record.revision != expected_revision + 1:
                raise ValueError("record revision must increment by one")  # noqa: TRY003  # lint-waiver: LW-930031 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
            self._records[record.handle_id] = record
            return record

    async def records(self) -> tuple[StoredEvaluation, ...]:
        """Return every durable record in stable handle order."""
        async with self._lock:
            return tuple(self._records[key] for key in sorted(self._records))

    async def nonterminal(self) -> tuple[StoredEvaluation, ...]:
        """Return every accepted, queued, or running evaluation."""
        terminal = {
            EvaluationState.SUCCEEDED,
            EvaluationState.FAILED,
            EvaluationState.CANCELED,
            EvaluationState.SUPERSEDED,
        }
        return tuple(record for record in await self.records() if record.state not in terminal)


@dataclass(frozen=True, slots=True)
class FakeSubmission:
    """One idempotent executor submission observed by a test."""

    handle_id: str
    request: EvaluationRequest


@dataclass
class FakeEvaluationBackend:
    """Shared remote execution state for one or more fake executor clients.

    Sharing this object across ``FakeEvaluationExecutor`` instances models a
    durable provider whose accepted work survives a client process restart.
    """

    submissions: list[FakeSubmission] = field(default_factory=list)
    _states: dict[str, ExecutorObservation] = field(default_factory=dict)
    _events: dict[str, asyncio.Event] = field(default_factory=dict)
    _active: set[str] = field(default_factory=set)

    @property
    def active_count(self) -> int:
        """Return the number of accepted nonterminal remote executions."""
        return len(self._active)

    def accept(self, handle_id: str, request: EvaluationRequest) -> bool:
        """Idempotently accept one queued remote execution."""
        if handle_id in self._states:
            return False
        self.submissions.append(FakeSubmission(handle_id, request))
        self._active.add(handle_id)
        self.publish(handle_id, ExecutorObservation(state=EvaluationState.QUEUED))
        return True

    def inspect(self, handle_id: str) -> ExecutorObservation | None:
        """Return the latest remote observation."""
        return self._states.get(handle_id)

    def publish(self, handle_id: str, observation: ExecutorObservation) -> None:
        """Persist a remote observation and wake waiting clients."""
        self._states[handle_id] = observation
        if observation.state in {
            EvaluationState.SUCCEEDED,
            EvaluationState.FAILED,
            EvaluationState.CANCELED,
            EvaluationState.SUPERSEDED,
        }:
            self._active.discard(handle_id)
        self.change_event(handle_id).set()

    def change_event(self, handle_id: str) -> asyncio.Event:
        """Return the sticky notification for one remote execution."""
        event = self._events.get(handle_id)
        if event is None:
            event = asyncio.Event()
            self._events[handle_id] = event
        return event


@dataclass(frozen=True, slots=True)
class _WaitAction:
    elapsed_s: float
    observation: ExecutorObservation | None = None


@dataclass
class FakeEvaluationExecutor:
    """Controllable execution port with event-driven waits and no sleeps."""

    clock: FakeClock
    availability_state: AvailabilityState = AvailabilityState.IMMEDIATE
    capacity: int | None = 1
    in_flight: int = 0
    queue_depth: int = 0
    estimated_start_after_s: float | None = 0.0
    estimated_runtime_s: float | None = None
    reuse_status: ReuseStatus = ReuseStatus.NONE
    cost_class: CostClass = CostClass.UNKNOWN
    fresh_for_s: float = 60.0
    supported_evidence_kinds: tuple[str, ...] = ()
    supported_capabilities: tuple[str, ...] = ()
    auto_cancel: bool = True
    fail_cancel_once: bool = False
    advance_clock_on_timeout: bool = True
    fail_after_accept_once: bool = False
    rejection: str | None = None
    backend: FakeEvaluationBackend = field(default_factory=FakeEvaluationBackend)
    wait_calls: list[tuple[str, float]] = field(default_factory=list)
    wait_started: asyncio.Event = field(default_factory=asyncio.Event)
    cancellations: list[str] = field(default_factory=list)
    _inspection_timeouts: int = 0
    _observations: deque[ExecutorObservation | None] = field(default_factory=deque)
    _wait_actions: deque[_WaitAction] = field(default_factory=deque)

    @property
    def submissions(self) -> list[FakeSubmission]:
        """Return the remote executions accepted by the shared fake backend."""
        return self.backend.submissions

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        """Return a capacity observation at current fake time."""
        del requirements
        return AvailabilitySnapshot(
            state=self.availability_state,
            capacity=self.capacity,
            in_flight=self.in_flight + self.backend.active_count,
            queue_depth=self.queue_depth,
            estimated_start_after_s=self.estimated_start_after_s,
            estimated_runtime_s=self.estimated_runtime_s,
            reuse_status=self.reuse_status,
            cost_class=self.cost_class,
            observed_at=self.clock.monotonic(),
            fresh_for_s=self.fresh_for_s,
            supported_evidence_kinds=self.supported_evidence_kinds,
            supported_capabilities=self.supported_capabilities,
        )

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        """Create one queued execution per stable handle ID, unless set to reject."""
        if self.rejection is not None:
            raise ExecutorRejectedError(self.rejection)
        if not self.backend.accept(handle_id, request):
            return
        if self.fail_after_accept_once:
            self.fail_after_accept_once = False
            raise ExecutorSubmissionError(OSError("submission response lost after acceptance"))

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        """Return the current executor state."""
        if self._inspection_timeouts:
            self._inspection_timeouts -= 1
            raise TimeoutError
        if self._observations:
            return self._observations.popleft()
        return self.backend.inspect(handle_id)

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        """Wait for a deliberate test state change or advance fake time."""
        self.wait_calls.append((handle_id, timeout_s))
        self.wait_started.set()
        event = self.backend.change_event(handle_id)
        if event.is_set():
            event.clear()
            return
        if self._wait_actions:
            action = self._wait_actions.popleft()
            self.clock.advance(min(action.elapsed_s, timeout_s))
            if action.observation is not None and action.elapsed_s <= timeout_s:
                self.set_observation(handle_id, action.observation)
            return
        if self.advance_clock_on_timeout:
            self.clock.advance(timeout_s)
            return
        await event.wait()
        event.clear()

    async def cancel(self, handle_id: str) -> None:
        """Optionally complete cancellation immediately for the Fake."""
        self.cancellations.append(handle_id)
        if self.fail_cancel_once:
            self.fail_cancel_once = False
            raise OSError
        if self.auto_cancel:
            self.set_state(handle_id, EvaluationState.CANCELED)

    def set_state(
        self,
        handle_id: str,
        state: EvaluationState,
        *,
        current_stage: str | None = None,
        stage_results: tuple[EvaluationStepResult, ...] = (),
        failure: str | None = None,
    ) -> None:
        """Publish an executor transition and wake waiters."""
        self.set_observation(
            handle_id,
            ExecutorObservation(
                state=state,
                current_stage=current_stage,
                stage_results=stage_results,
                failure=failure,
            ),
        )

    def set_observation(self, handle_id: str, observation: ExecutorObservation) -> None:
        """Publish a faithful remote observation and wake waiting clients."""
        self.backend.publish(handle_id, observation)

    def timeout_next_inspections(self, count: int = 1) -> None:
        """Make the next ``count`` provider observations time out immediately."""
        if count <= 0:
            raise ValueError("inspection timeout count must be positive")  # noqa: TRY003  # lint-waiver: LW-930032 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        self._inspection_timeouts += count

    def script_observations(self, *observations: ExecutorObservation | None) -> None:
        """Return exact observations on upcoming inspections without mutating remote state."""
        self._observations.extend(observations)

    def script_wait_timeout(self, elapsed_s: float, *, count: int = 1) -> None:
        """Make upcoming change waits consume fake time without a state change."""
        if elapsed_s < 0 or count <= 0:
            raise ValueError("wait timeout duration must be nonnegative and count positive")  # noqa: TRY003  # lint-waiver: LW-930033 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        self._wait_actions.extend(_WaitAction(elapsed_s) for _ in range(count))

    def script_wait_transition(
        self,
        observation: ExecutorObservation,
        *,
        elapsed_s: float = 0,
    ) -> None:
        """Publish a remote transition during the next scripted change wait."""
        if elapsed_s < 0:
            raise ValueError("wait transition duration must be nonnegative")  # noqa: TRY003  # lint-waiver: LW-930034 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        self._wait_actions.append(_WaitAction(elapsed_s, observation))


class InMemoryEvaluationNamespace:
    """Store serialized models, preserving strict reads and replacement atomicity."""

    def __init__(self) -> None:
        """Start with no external operational state."""
        self._values: dict[str, str] = {}

    def load[ModelT: BaseModel](
        self, relative_path: str | PurePosixPath, model_type: type[ModelT]
    ) -> ModelT:
        """Return a detached, strictly validated required model."""
        key = self._key(relative_path)
        if key not in self._values:
            raise StateModelNotFoundError.missing(Path(key))
        try:
            return model_type.model_validate_json(self._values[key], strict=True)
        except ValidationError as error:
            failures = [
                f"{'.'.join(str(part) for part in detail['loc']) or 'metadata'}: {detail['msg']}"
                for detail in error.errors(
                    include_url=False, include_context=False, include_input=False
                )
            ]
            raise ProjectStateError.invalid_state_model(Path(key), "; ".join(failures)) from error

    def load_optional[ModelT: BaseModel](
        self, relative_path: str | PurePosixPath, model_type: type[ModelT]
    ) -> ModelT | None:
        """Return None only when the model is absent."""
        if self._key(relative_path) not in self._values:
            return None
        return self.load(relative_path, model_type)

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        """Atomically replace the serialized model without retaining caller aliases."""
        key = self._key(relative_path)
        try:
            serialized = json.dumps(model.model_dump(mode="json", round_trip=True), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ProjectStateError.state_serialization_failed() from error
        self._values[key] = serialized

    def delete(self, relative_path: str | PurePosixPath) -> bool:
        """Delete an existing model, reporting whether it was present."""
        return self._values.pop(self._key(relative_path), None) is not None

    @staticmethod
    def _key(relative_path: str | PurePosixPath) -> str:
        path = PurePosixPath(relative_path)
        if (
            not str(relative_path)
            or (
                isinstance(relative_path, str)
                and any(not part for part in relative_path.split("/"))
            )
            or path.is_absolute()
            or not path.parts
            or ".." in path.parts
            or "\\" in str(relative_path)
        ):
            raise ProjectStateError.invalid_state_file_path(relative_path, portable=True)
        return path.as_posix()


__all__ = [
    "FakeClock",
    "FakeDeadlineFactory",
    "FakeDeadlineScope",
    "FakeEvaluationBackend",
    "FakeEvaluationExecutor",
    "FakeSubmission",
    "InMemoryEvaluationNamespace",
    "InMemoryEvaluationStore",
]
