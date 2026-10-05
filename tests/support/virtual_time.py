"""One virtual clock for host tests: waiting costs no wall time and runs everything due.

``VirtualClock`` is the run clock (``vs_runtime`` ``RunClock``), the Fake Slurm cluster's
clock (``vs_slurm`` ``Clock``) and the timeline agent-turn durations are slept on. It is
backed by an event loop whose time *is* the clock: ``run_virtual`` runs a coroutine on a
loop that never blocks on a timer. When every task is waiting, the loop jumps the clock
to the earliest timer and runs what is due there. Because a jump happens only when no
task is runnable, every waiter shares one timeline: sleeps overlap the way real ones do,
and any number of tasks can wait at once without deadlocking each other.

Everything on that loop must wait through the loop or the clock (``asyncio.sleep``,
``clock.sleep``). A task blocked on a real thread or process would be invisible to the
jump, so the loop refuses to idle with nothing scheduled instead of hanging.
"""

from __future__ import annotations

import asyncio
import selectors
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine


class VirtualDeadlockError(RuntimeError):
    """Every task is waiting and no timer is scheduled: nothing can ever wake the run."""


class _VirtualSelector(selectors.BaseSelector):
    """The loop's selector, with timer waits turned into clock jumps."""

    def __init__(self, clock: VirtualClock) -> None:
        self._clock = clock
        self._real = selectors.DefaultSelector()

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        ready = self._real.select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            message = "every task is waiting and no timer is scheduled"
            raise VirtualDeadlockError(message)
        self._clock.at += timeout
        return []

    def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:  # noqa: ANN401  # mirrors selectors.BaseSelector.register
        return self._real.register(fileobj, events, data)

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:  # noqa: ANN401  # mirrors selectors.BaseSelector.unregister
        return self._real.unregister(fileobj)

    def modify(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:  # noqa: ANN401  # mirrors selectors.BaseSelector.modify
        return self._real.modify(fileobj, events, data)

    def get_map(self) -> Any:  # noqa: ANN401  # mirrors selectors.BaseSelector.get_map
        return self._real.get_map()

    def close(self) -> None:
        self._real.close()


class _VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self, clock: VirtualClock) -> None:
        super().__init__(_VirtualSelector(clock))
        self._clock = clock

    def time(self) -> float:
        return self._clock.at


class VirtualClock:
    """Seconds on the shared virtual timeline. ``sleep`` waits for the timeline, not the wall."""

    def __init__(self, at: float = 1.0) -> None:
        """Start the timeline at ``at`` seconds."""
        self.at = at

    def now(self) -> float:
        """The current virtual time."""
        return self.at

    async def sleep(self, seconds: float) -> None:
        """Wait until the virtual time is ``seconds`` later; other tasks run meanwhile."""
        await asyncio.sleep(seconds)


def run_virtual[T](clock: VirtualClock, main: Coroutine[Any, Any, T]) -> T:
    """Run ``main`` to completion on a loop whose time is ``clock``."""
    loop = _VirtualLoop(clock)
    try:
        return loop.run_until_complete(main)
    finally:
        loop.close()
