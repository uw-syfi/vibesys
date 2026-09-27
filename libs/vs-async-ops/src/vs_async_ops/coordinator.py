"""Durable asynchronous operation lifecycle mechanism."""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from vs_async_ops.models import (
    OperationCompleted,
    OperationHandle,
    OperationLifecycleEvent,
    OperationPolicy,
    OperationRequest,
    OperationState,
    OperationTimedOut,
)
from vs_async_ops.ports import NULL_OPERATION_EVENT_SINK

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from pydantic import JsonValue

    from vs_async_ops.models import OperationAwaitResult
    from vs_async_ops.ports import (
        OperationDeadlineScope,
        OperationEventErrorSink,
        OperationEventSink,
        OperationRunner,
        OperationStore,
        OperationWaiter,
    )

_LOG = logging.getLogger(__name__)


class AsyncioOperationWaiter:
    """Production bounded wait based on the asyncio event loop."""

    async def wait(self, event: asyncio.Event, timeout_s: float) -> bool:
        """Wait using asyncio and report deadline expiry as false."""
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
        except TimeoutError:
            return False
        return True


class OperationNotFoundError(KeyError):
    """An operation ID has no durable record."""


class OperationCoordinatorClosedError(RuntimeError):
    """New work was submitted after coordinator shutdown began."""

    def __init__(self) -> None:
        """Build the fixed shutdown diagnostic."""
        super().__init__("operation coordinator is closed")


class OperationObservationTimeoutError(TimeoutError):
    """The deadline expired before any authoritative record was observed."""

    def __init__(self, operation_id: str) -> None:
        """Name the operation whose record could not be observed in time."""
        super().__init__(f"operation {operation_id!r} could not be observed before the deadline")


class OperationCancellationTimeoutError(TimeoutError):
    """Cancellation exceeded policy after durable termination was recorded."""

    def __init__(self, operation_id: str) -> None:
        """Name the operation whose cancellation did not finish in time."""
        super().__init__(f"operation {operation_id!r} cancellation exceeded its deadline")


class InvalidOperationTimeoutError(ValueError):
    """A bounded wait has an invalid timeout."""

    def __init__(self, maximum: float | None) -> None:
        """Name the optional configured maximum."""
        suffix = f" and at most {maximum}" if maximum is not None else ""
        super().__init__(f"timeout_s must be a positive finite number{suffix}")


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class _LockPool:
    """Reference-counted keyed locks that retire after their final user."""

    def __init__(self) -> None:
        self._entries: dict[str, _LockEntry] = {}
        self._guard = asyncio.Lock()

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        async with self._guard:
            entry = self._entries.setdefault(key, _LockEntry(asyncio.Lock()))
            entry.users += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(key) is entry:
                    del self._entries[key]


def _report_event_error(error: Exception, event: OperationLifecycleEvent) -> None:
    _LOG.error(
        "operation lifecycle event sink failed for %s revision %s",
        event.operation_id,
        event.revision,
        exc_info=error,
    )


class OperationCoordinator:
    """Persist before scheduling and serialize work by concurrency key."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-930001 [PLR0913]; these arguments are independent injected ports or policy facts; grouping them in a DTO would add a shallow mutable carrier and obscure ownership.
        self,
        runner: OperationRunner,
        store: OperationStore,
        *,
        waiter: OperationWaiter | None = None,
        deadline_factory: Callable[[float], OperationDeadlineScope] | None = None,
        events: OperationEventSink = NULL_OPERATION_EVENT_SINK,
        event_errors: OperationEventErrorSink = _report_event_error,
        policy: OperationPolicy | None = None,
    ) -> None:
        """Bind injected effects and lifecycle policy."""
        self._runner = runner
        self._store = store
        self._waiter = waiter or AsyncioOperationWaiter()
        self._deadline_factory = deadline_factory or cast(
            "Callable[[float], OperationDeadlineScope]", asyncio.timeout
        )
        self._events = events
        self._event_errors = event_errors
        policy = policy or OperationPolicy()
        self._max_await_timeout_s = policy.max_await_timeout_s
        self._cancellation_timeout_s = policy.cancellation_timeout_s
        self._admission = (
            asyncio.Semaphore(policy.max_in_flight) if policy.max_in_flight is not None else None
        )
        self._start_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._key_locks = _LockPool()
        self._operation_locks = _LockPool()
        self._changed: dict[str, asyncio.Event] = {}
        self._latest: dict[str, OperationHandle] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        """Interrupt work orphaned by process restart, once."""
        async with self._start_lock:
            if self._started:
                return
            for record in await self._store.records():
                if not record.state.terminal:
                    self._latest[record.request.operation_id] = record
                    await self._transition(record, OperationState.INTERRUPTED)
            self._started = True

    async def submit(self, request: OperationRequest) -> OperationHandle:
        """Durably accept work and return before its runner completes."""
        await self.start()
        async with self._lifecycle_lock:
            if self._closed:
                raise OperationCoordinatorClosedError
            record = OperationHandle(request=request, state=OperationState.QUEUED)
            await self._store.create(record)
            self._latest[request.operation_id] = record
            self._publish(record)
            self._changed.setdefault(request.operation_id, asyncio.Event())
            self._tasks[request.operation_id] = asyncio.create_task(self._execute(request))
            return record

    async def status(self, operation_id: str) -> OperationHandle:
        """Return the durable current record."""
        await self.start()
        record = await self._store.get(operation_id)
        if record is None:
            raise OperationNotFoundError(operation_id)
        if record.state.terminal:
            self._latest.pop(operation_id, None)
        else:
            self._latest[operation_id] = record
        return record

    async def records(self) -> tuple[OperationHandle, ...]:
        """Return every durable operation without changing its state."""
        await self.start()
        return await self._store.records()

    async def await_result(self, operation_id: str, timeout_s: float) -> OperationAwaitResult:
        """Wait at most ``timeout_s`` without changing operation state."""
        self._validate_timeout(timeout_s)
        deadline = self._deadline_factory(timeout_s)
        try:
            async with deadline:
                await self.start()
                record = await self.status(operation_id)
                if not record.state.terminal:
                    event = self._changed.setdefault(operation_id, asyncio.Event())
                    event.clear()
                    record = await self.status(operation_id)
                    if not record.state.terminal:
                        await self._waiter.wait(event, timeout_s)
                        record = await self.status(operation_id)
                if record.state.terminal:
                    return OperationCompleted(record=record)
                return OperationTimedOut(record=record)
        except TimeoutError:
            if not deadline.expired():
                raise
            record = self._latest.get(operation_id)
            if record is None:
                raise OperationObservationTimeoutError(operation_id) from None
            return OperationTimedOut(record=record)

    async def cancel(self, operation_id: str) -> OperationHandle:
        """Request cancellation and durably make the operation terminal."""
        await self.start()
        return await self._terminate(operation_id, OperationState.CANCELED)

    async def close(self) -> None:
        """Reject new work, then interrupt and cancel work owned here."""
        await self.start()
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
        first_error: Exception | None = None
        for record in await self._store.records():
            if record.state.terminal:
                continue
            try:
                await self._terminate(record.request.operation_id, OperationState.INTERRUPTED)
            except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-930002 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
                if first_error is None:
                    first_error = exc
        self._tasks.clear()
        if first_error is not None:
            raise first_error

    async def _terminate(
        self, operation_id: str, terminal_state: OperationState
    ) -> OperationHandle:
        async with self._operation_locks.hold(operation_id):
            record = await self.status(operation_id)
            if record.state.terminal:
                return record
            cancel_error: Exception | None = None
            task = self._tasks.get(operation_id)
            try:
                await self._stop_execution(record, task)
            except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-930003 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
                cancel_error = exc
            current = await self.status(operation_id)
            result = (
                current
                if current.state.terminal
                else await self._transition(current, terminal_state)
            )
            if cancel_error is not None:
                raise cancel_error
            return result

    async def _stop_execution(
        self, record: OperationHandle, task: asyncio.Task[None] | None
    ) -> None:
        async def stop() -> None:
            runner_error: Exception | None = None
            if record.state is OperationState.RUNNING:
                try:
                    await self._runner.cancel(record.request.operation_id)
                except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-930004 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
                    runner_error = exc
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if runner_error is not None:
                raise runner_error

        if self._cancellation_timeout_s is None:
            await stop()
            return
        deadline = self._deadline_factory(self._cancellation_timeout_s)
        try:
            async with deadline:
                await stop()
        except TimeoutError:
            if not deadline.expired():
                raise
            if task is not None:
                task.cancel()
            raise OperationCancellationTimeoutError(record.request.operation_id) from None

    async def _execute(self, request: OperationRequest) -> None:
        admitted = False
        try:
            async with self._key_locks.hold(request.concurrency_key):
                if self._admission is not None:
                    await self._admission.acquire()
                    admitted = True
                async with self._lifecycle_lock:
                    if self._closed:
                        return
                    async with self._operation_locks.hold(request.operation_id):
                        record = await self.status(request.operation_id)
                        if record.state is not OperationState.QUEUED:
                            return
                        await self._transition(record, OperationState.RUNNING)
                try:
                    result = await self._runner.run(request)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-930005 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
                    async with self._operation_locks.hold(request.operation_id):
                        current = await self.status(request.operation_id)
                        if not current.state.terminal:
                            await self._transition(
                                current,
                                OperationState.FAILED,
                                failure=f"{type(exc).__name__}: {exc}",
                            )
                else:
                    async with self._operation_locks.hold(request.operation_id):
                        current = await self.status(request.operation_id)
                        if current.state is OperationState.RUNNING:
                            await self._transition(
                                current,
                                OperationState.SUCCEEDED,
                                result=result,
                                result_present=True,
                            )
        finally:
            if self._admission is not None and admitted:
                self._admission.release()
            self._tasks.pop(request.operation_id, None)

    async def _transition(
        self,
        record: OperationHandle,
        state: OperationState,
        *,
        result: JsonValue | None = None,
        result_present: bool = False,
        failure: str | None = None,
    ) -> OperationHandle:
        updated = OperationHandle(
            request=record.request,
            state=state,
            result=result,
            result_present=result_present,
            failure=failure,
            revision=record.revision + 1,
        )
        try:
            await self._store.replace(updated, expected_revision=record.revision)
        except Exception:
            current = await self._store.get(record.request.operation_id)
            if current is not None and current.state.terminal:
                return current
            raise
        self._latest[record.request.operation_id] = updated
        self._publish(updated)
        if updated.state.terminal:
            event = self._changed.pop(record.request.operation_id, None)
            if event is not None:
                event.set()
            self._latest.pop(record.request.operation_id, None)
        return updated

    def _publish(self, record: OperationHandle) -> None:
        event = OperationLifecycleEvent(
            operation_id=record.request.operation_id,
            concurrency_key=record.request.concurrency_key,
            state=record.state,
            revision=record.revision,
        )
        try:
            self._events(event)
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-930006 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
            try:
                self._event_errors(exc, event)
            except Exception:  # noqa: BLE001  # lint-waiver: LW-930007 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
                _LOG.exception("operation lifecycle error reporter failed")

    def _validate_timeout(self, timeout_s: float) -> None:
        if (
            isinstance(timeout_s, bool)
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
            or (self._max_await_timeout_s is not None and timeout_s > self._max_await_timeout_s)
        ):
            raise InvalidOperationTimeoutError(self._max_await_timeout_s)


__all__ = [
    "AsyncioOperationWaiter",
    "InvalidOperationTimeoutError",
    "OperationCancellationTimeoutError",
    "OperationCoordinator",
    "OperationCoordinatorClosedError",
    "OperationNotFoundError",
    "OperationObservationTimeoutError",
]
