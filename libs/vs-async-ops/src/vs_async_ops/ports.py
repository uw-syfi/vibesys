"""Injected effects required by the operation coordinator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from asyncio import Event
    from types import TracebackType

    from pydantic import JsonValue

    from vs_async_ops.models import OperationHandle, OperationLifecycleEvent, OperationRequest


class OperationRunner(Protocol):
    """Execute and cancel opaque work by stable operation ID."""

    async def run(self, request: OperationRequest) -> JsonValue:
        """Run one operation and return its opaque JSON result."""
        ...

    async def cancel(self, operation_id: str) -> None:
        """Request cancellation idempotently."""
        ...


class OperationStore(Protocol):
    """Durable atomic record storage."""

    async def create(self, record: OperationHandle) -> None:
        """Insert a record or reject an existing operation ID."""
        ...

    async def get(self, operation_id: str) -> OperationHandle | None:
        """Read one record."""
        ...

    async def replace(self, record: OperationHandle, *, expected_revision: int) -> None:
        """Replace iff the durable revision equals ``expected_revision``."""
        ...

    async def records(self) -> tuple[OperationHandle, ...]:
        """Read every record."""
        ...


class OperationWaiter(Protocol):
    """Wait mechanism, injectable for deterministic timeout tests."""

    async def wait(self, event: Event, timeout_s: float) -> bool:
        """Return true when signaled, false when the observation bound expires."""
        ...


class OperationEventSink(Protocol):
    """Observe persisted lifecycle transitions."""

    def __call__(self, event: OperationLifecycleEvent, /) -> None:
        """Publish one lifecycle fact."""
        ...


class OperationEventErrorSink(Protocol):
    """Report a lifecycle observer failure without changing operation state."""

    def __call__(self, error: Exception, event: OperationLifecycleEvent, /) -> None:
        """Observe one rejected lifecycle event."""
        ...


class OperationDeadlineScope(Protocol):
    """Cancelable async scope that identifies its own deadline expiry."""

    async def __aenter__(self) -> object:
        """Begin the bounded operation."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Translate deadline cancellation into ``TimeoutError``."""
        ...

    def expired(self) -> bool:
        """Return whether this scope's deadline caused the timeout."""
        ...


class NullOperationEventSink:
    """Discard lifecycle events."""

    def __call__(self, event: OperationLifecycleEvent, /) -> None:
        """Discard one event."""
        del event


NULL_OPERATION_EVENT_SINK = NullOperationEventSink()


__all__ = [
    "NULL_OPERATION_EVENT_SINK",
    "NullOperationEventSink",
    "OperationDeadlineScope",
    "OperationEventErrorSink",
    "OperationEventSink",
    "OperationRunner",
    "OperationStore",
    "OperationWaiter",
]
