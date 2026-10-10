"""Wait for a held operation to start without hanging when it never does."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from vs_evaluation.api import EvaluationState

if TYPE_CHECKING:
    import threading
    from collections.abc import Awaitable

    from vs_evaluation.api import EvaluationExecutor


async def wait_until_started(
    started: threading.Event | asyncio.Event, operation: asyncio.Future[Any]
) -> None:
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
    if isinstance(started, asyncio.Event):
        await started.wait()
    else:
        await asyncio.to_thread(started.wait)
    if operation.done():
        operation.result()
        message = "the operation finished without reaching its held step"
        raise AssertionError(message)


_ENDED = frozenset(
    {
        EvaluationState.SUCCEEDED,
        EvaluationState.FAILED,
        EvaluationState.CANCELED,
        EvaluationState.SUPERSEDED,
    }
)


async def _ended(executor: EvaluationExecutor, handle_id: str) -> None:
    while True:
        observed = await executor.inspect_only(handle_id)
        if observed is not None and observed.state in _ENDED:
            return
        await executor.wait_for_change(handle_id, timeout_s=float("inf"))
        # A fake may return at once; yield so the test's own tasks still run.
        await asyncio.sleep(0)


async def wait_until_executor_started(
    started: threading.Event | asyncio.Event, executor: EvaluationExecutor, handle_id: str
) -> None:
    """Return once *started* is set; fail if the evaluation *handle_id* ends first.

    For a held step that a worker inside the executor starts, where the test has
    no handle on the operation itself: the evaluation reaching a terminal state
    stands in for the operation ending, so a start that never comes fails the
    test with the evaluation's end and not with a parked worker. A held step
    keeps the evaluation live, so one that is already ended when the start is
    observed was not held.

    Raises:
        AssertionError: the evaluation ended without reaching its held step.
    """
    watcher = asyncio.ensure_future(_ended(executor, handle_id))
    try:
        await wait_until_started(started, watcher)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


async def arrival[T](awaited: Awaitable[T], *operations: asyncio.Future[Any]) -> T:
    """Await *awaited* (an event wait or queue get) unless an operation ends first.

    For a test body that waits for something a task it started must produce
    (``await session.started.wait()``, ``await queue.get()``). If an operation
    in *operations* ends before *awaited* completes, the producer can no longer
    deliver, so this raises that operation's own error, or an assertion when it
    returned. An arrival that is already there wins over an operation that has
    also ended, so a producer that arrives and then finishes is not an error.

    Raises:
        AssertionError: an operation returned without the awaited arrival.
        BaseException: whatever an operation raised before the arrival.
    """
    waiter = asyncio.ensure_future(awaited)
    try:
        await asyncio.wait({waiter, *operations}, return_when=asyncio.FIRST_COMPLETED)
        if waiter.done():
            return waiter.result()
        for operation in operations:
            if operation.done():
                operation.result()
        message = "an operation finished without the arrival the test waits for"
        raise AssertionError(message)
    finally:
        if not waiter.done():
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
