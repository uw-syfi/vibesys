"""Wait for a state to hold by waiting for change notifications, not by polling."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class Changes:
    """A counter of change notifications that a waiter cannot miss.

    A waiter reads ``version`` *before* it observes the state; ``changed(since)`` then
    returns at once if a notification arrived in between. The state's owner calls
    ``notify`` after every change to what ``observe`` reads.
    """

    def __init__(self) -> None:
        """Start with no notifications."""
        self.version = 0
        self._waiters: list[asyncio.Future[None]] = []

    def notify(self) -> None:
        """Record a change and wake every waiter."""
        self.version += 1
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def changed(self, since: int) -> None:
        """Return once ``version`` differs from ``since``."""
        while self.version == since:
            waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            await waiter


async def wait_for_state[T](
    observe: Callable[[], T], satisfied: Callable[[T], bool], changes: Changes
) -> T:
    """Return the first observed state for which ``satisfied`` holds, re-observing after each change.

    There is no timeout: a state that can never hold is a deadlock, which the virtual
    loop reports at once.
    """
    while True:
        seen = changes.version
        value = observe()
        if satisfied(value):
            return value
        await changes.changed(seen)


async def wait_for_async_state[T](
    observe: Callable[[], Awaitable[T]], satisfied: Callable[[T], bool], changes: Changes
) -> T:
    """:func:`wait_for_state` for an observation that is itself a coroutine."""
    while True:
        seen = changes.version
        value = await observe()
        if satisfied(value):
            return value
        await changes.changed(seen)
