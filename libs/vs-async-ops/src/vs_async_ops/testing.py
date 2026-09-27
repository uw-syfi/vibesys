"""Public deterministic fakes for asynchronous operation clients."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType

    from pydantic import JsonValue

    from vs_async_ops.models import OperationHandle, OperationRequest


class _FakeStoreError(RuntimeError):
    def __init__(self, operation_id: str, reason: str) -> None:
        super().__init__(f"operation {operation_id!r} {reason}")


class InMemoryOperationStore:
    """Atomic in-memory implementation of the durable store contract."""

    def __init__(self, records: tuple[OperationHandle, ...] = ()) -> None:
        """Seed optional durable records."""
        self._records = {item.request.operation_id: item for item in records}
        self._lock = asyncio.Lock()

    async def create(self, record: OperationHandle) -> None:
        """Insert a uniquely identified record."""
        async with self._lock:
            operation_id = record.request.operation_id
            if operation_id in self._records:
                raise _FakeStoreError(operation_id, "already exists")
            self._records[operation_id] = record

    async def get(self, operation_id: str) -> OperationHandle | None:
        """Return one record when present."""
        async with self._lock:
            return self._records.get(operation_id)

    async def replace(self, record: OperationHandle, *, expected_revision: int) -> None:
        """Replace one record at the expected revision."""
        async with self._lock:
            operation_id = record.request.operation_id
            current = self._records.get(operation_id)
            if current is None or current.revision != expected_revision:
                raise _FakeStoreError(operation_id, "revision conflict")
            self._records[operation_id] = record

    async def records(self) -> tuple[OperationHandle, ...]:
        """Return all records."""
        async with self._lock:
            return tuple(self._records.values())


class FakeOperationRunner:
    """Controllable faithful runner that records concurrency."""

    def __init__(self) -> None:
        """Start with no released operations."""
        self.started: list[str] = []
        self.canceled: list[str] = []
        self.active: set[str] = set()
        self.max_active = 0
        self._release: dict[str, asyncio.Event] = {}
        self._started: dict[str, asyncio.Event] = {}
        self._results: dict[str, JsonValue] = {}

    async def run(self, request: OperationRequest) -> JsonValue:
        """Record and block one operation until explicitly completed."""
        operation_id = request.operation_id
        self.started.append(operation_id)
        self._started.setdefault(operation_id, asyncio.Event()).set()
        self.active.add(operation_id)
        self.max_active = max(self.max_active, len(self.active))
        release = self._release.setdefault(operation_id, asyncio.Event())
        try:
            await release.wait()
            return self._results[operation_id]
        finally:
            self.active.discard(operation_id)

    async def cancel(self, operation_id: str) -> None:
        """Record and release one cancellation request."""
        self.canceled.append(operation_id)
        self._release.setdefault(operation_id, asyncio.Event()).set()

    def complete(self, operation_id: str, result: JsonValue) -> None:
        """Release one pending run with ``result``."""
        self._results[operation_id] = result
        self._release.setdefault(operation_id, asyncio.Event()).set()

    async def wait_started(self, operation_id: str) -> None:
        """Yield until the requested operation has entered ``run``."""
        await self._started.setdefault(operation_id, asyncio.Event()).wait()


class ImmediateTimeoutWaiter:
    """Deterministically report an observational timeout."""

    async def wait(self, event: asyncio.Event, timeout_s: float) -> bool:
        """Return false immediately without mutating the event."""
        del event, timeout_s
        return False


class TimeoutOnceWaiter:
    """Timeout the first observation, then faithfully await later signals."""

    def __init__(self) -> None:
        """Start before the one scripted timeout."""
        self._timed_out = False

    async def wait(self, event: asyncio.Event, timeout_s: float) -> bool:
        """Return false once, then wait for the operation event."""
        del timeout_s
        if not self._timed_out:
            self._timed_out = True
            return False
        await event.wait()
        return True


class ObservingWaiter:
    """Expose when a real signal-only wait begins."""

    def __init__(self) -> None:
        """Create the externally observable entry event."""
        self.entered = asyncio.Event()

    async def wait(self, event: asyncio.Event, timeout_s: float) -> bool:
        """Wait for the lifecycle signal without consulting wall-clock time."""
        del timeout_s
        self.entered.set()
        await event.wait()
        return True


@dataclass
class FakeOperationDeadlineScope:
    """Manually expire an active bounded operation without wall-clock waits."""

    requested_s: float
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[object] | None = None
    _expired: bool = False

    async def __aenter__(self) -> FakeOperationDeadlineScope:
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
        """Turn a manually expired cancellation into ``TimeoutError``."""
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
            message = "deadline scope is not active"
            raise RuntimeError(message)
        self._expired = True
        self._task.cancel()


@dataclass
class FakeOperationDeadlineFactory:
    """Create inspectable manual deadline scopes for coordinator tests."""

    scopes: list[FakeOperationDeadlineScope] = field(default_factory=list)

    def __call__(self, timeout_s: float) -> FakeOperationDeadlineScope:
        """Retain one scope with the requested bound."""
        scope = FakeOperationDeadlineScope(timeout_s)
        self.scopes.append(scope)
        return scope


__all__ = [
    "FakeOperationDeadlineFactory",
    "FakeOperationDeadlineScope",
    "FakeOperationRunner",
    "ImmediateTimeoutWaiter",
    "InMemoryOperationStore",
    "ObservingWaiter",
    "TimeoutOnceWaiter",
]
