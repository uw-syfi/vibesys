"""Wait for a held operation to start without hanging when it never does."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading


async def wait_until_started(started: threading.Event, operation: asyncio.Future[object]) -> None:
    """Return once *started* is set; surface the outcome of *operation* if it ends first.

    A test that parks a worker on ``started.wait`` hangs forever when the
    operation fails or returns before it reaches the point that sets *started*,
    and ``asyncio.run`` then waits on that worker at shutdown. Ending
    *operation* releases the waiter here, so the test fails with the
    operation's own error instead. *started* is set on that path, so callers
    must not treat it as proof of a start without this function's return.

    Raises:
        AssertionError: *operation* returned without ever starting.
        BaseException: whatever *operation* raised before it started.
    """
    operation.add_done_callback(lambda _done: started.set())
    await asyncio.to_thread(started.wait)
    if operation.done():
        operation.result()
        message = "the operation finished without reaching its held step"
        raise AssertionError(message)
