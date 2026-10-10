"""Waits tied to the lifetime of what they wait on.

A test that parks on ``event.wait()`` for something a task will do hangs forever when
that task fails or returns first, and ``asyncio.run`` then waits on the parked worker at
shutdown. A :class:`Gate` (or :func:`arrival`) names the operations that could open it:
when one of them ends first, the wait ends with that operation's own error.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_sim.concurrency import Event


async def arrival[T](awaited: Awaitable[T], *operations: asyncio.Future[Any]) -> T:
    """Await *awaited* (an event wait or queue get) unless an operation ends first.

    If an operation in *operations* ends before *awaited* completes, the producer can no
    longer deliver, so this raises that operation's own error, or an assertion when it
    returned. An arrival that is already there wins over an operation that has also
    ended, so a producer that arrives and then finishes is not an error.

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


class Gate:
    """A one-way latch that code under test opens and a test waits on.

    ``open`` may be called from any thread and any number of times. ``wait`` returns
    once the gate is open; given the operations that should open it, it fails with an
    operation's own outcome if one ends first.
    """

    def __init__(self) -> None:
        """Create a closed gate."""
        self._lock = threading.Lock()
        self._open = False
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    @property
    def is_open(self) -> bool:
        """Whether the gate has been opened."""
        with self._lock:
            return self._open

    def open(self) -> None:
        """Open the gate and release every waiter."""
        with self._lock:
            self._open = True
            waiters, self._waiters = self._waiters, []
        for loop, future in waiters:
            _release(loop, future)

    async def wait(self, *operations: asyncio.Future[Any]) -> None:
        """Return once the gate is open, or fail with the outcome of an operation that ends first.

        Raises:
            AssertionError: an operation returned without the gate opening.
            BaseException: whatever an operation raised before the gate opened.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        with self._lock:
            if self._open:
                return
            self._waiters.append((loop, future))
        try:
            await arrival(future, *operations)
        finally:
            with self._lock:
                self._waiters = [entry for entry in self._waiters if entry[1] is not future]


def _release(loop: asyncio.AbstractEventLoop, future: asyncio.Future[None]) -> None:
    def resolve() -> None:
        if not future.done():
            future.set_result(None)

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        resolve()
    elif not loop.is_closed():
        loop.call_soon_threadsafe(resolve)


async def wait_until_started(
    started: Event | asyncio.Event, operation: asyncio.Future[Any]
) -> None:
    """Return once *started* is set; surface the outcome of *operation* if it ends first.

    *started* is set on the operation's end too, so callers must not treat it as proof of
    a start without this function's return.

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


def wait_until_started_sync(started: Event, operation: concurrent.futures.Future[Any]) -> None:
    """Return once *started* is set; surface the outcome of *operation* if it ends first.

    The test-thread form of :func:`wait_until_started`: a bare ``started.wait()``
    parks the test forever when the worker running *operation* fails or returns
    before it reaches the point that sets *started*. Ending *operation* releases
    the wait, so *started* is set on that path and a return from here, not the
    event, is the proof of a start.

    Raises:
        AssertionError: *operation* returned without ever starting.
        BaseException: whatever *operation* raised before it started.
    """
    operation.add_done_callback(lambda _done: started.set())
    started.wait()
    if operation.done():
        operation.result()
        message = "the operation finished without reaching its held step"
        raise AssertionError(message)


def start_thread[T](target: Callable[[], T]) -> concurrent.futures.Future[T]:
    """Run *target* on its own thread and return a future for its outcome.

    For a test that waits on a worker's progress with :func:`wait_until_started_sync`
    and has no pool to submit to. The thread is a daemon so that a stuck target
    never keeps the interpreter alive.
    """
    future: concurrent.futures.Future[T] = concurrent.futures.Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(target())
        except BaseException as error:  # noqa: BLE001  # LW-159901 [BLE001]; relayed to the waiter through the future.
            future.set_exception(error)

    threading.Thread(target=run, daemon=True).start()
    return future
