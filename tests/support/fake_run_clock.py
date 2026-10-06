"""A run clock that never waits: ``sleep`` advances a counter and records the request."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vs_runtime.api.core import HEARTBEAT_TASK, WAIT_TASK

#: Event-loop turns ``sleep`` lets background tasks run before logical time moves.
SETTLE_TURNS = 200

if TYPE_CHECKING:
    from collections.abc import Callable


class ClockLimitError(RuntimeError):
    """A simulated run waited past ``FakeRunClock.limit``: it is stuck, not slow."""


class HostCrashedError(RuntimeError):
    """The simulated host process died (see ``FakeRunClock.crash_on_next_clock_call``)."""


def _in_heartbeat() -> bool:
    """Whether the caller is the lease heartbeat task (false outside an event loop)."""
    try:
        current = asyncio.current_task()
    except RuntimeError:
        return False
    return current is not None and current.get_name() == HEARTBEAT_TASK


@dataclass
class FakeRunClock:
    """Logical seconds on the shared timeline. ``sleep`` moves time forward by its argument.

    A real sleep lets the event loop run the executors' background work (a Slurm submission
    runs as a task beside the loop). The fake keeps that: while logical time passes, every
    other task the loop is running finishes first, so a job submitted before a sleep has
    been accepted by the scheduler after it, without any wall-clock wait.

    ``settle_background=False`` is for a run whose own long-lived tasks (an event collector,
    the run session) are alive during every sleep: waiting for all of them would never
    return. ``sleep`` then yields to the loop once and advances time.

    ``limit`` turns a run that waits forever for a state that never comes into an error:
    simulated time is free, so a stuck run would otherwise spin until its deadline.
    """

    at: float = 0.0
    sleeps: list[float] = field(default_factory=list)
    settle_background: bool = True
    limit: float | None = None
    _crash_armed: bool = False
    _aftermath: Callable[[], None] | None = None

    def crash_on_next_clock_call(self, *, aftermath: Callable[[], None] | None = None) -> None:
        """Kill the simulated host at the loop's next clock read or wait, after what it committed.

        The run raises :class:`HostCrashedError` from that call, so nothing the host would
        have done afterwards happens. A scenario arms it from inside a scripted agent turn
        to choose the commit the crash follows. ``aftermath`` runs at the moment of death,
        to leave behind what a dying process leaves (for example an unreadable file).
        """
        self._crash_armed = True
        self._aftermath = aftermath

    def now(self) -> float:
        """The current logical time."""
        self._crash_if_armed("a clock read")
        return self.at

    def _crash_if_armed(self, where: str) -> None:
        # The lease heartbeat runs beside an in-flight agent turn: a crash it took would
        # land before the turn's reply is committed, not at the loop's next step.
        if _in_heartbeat():
            return
        if self._crash_armed:
            self._crash_armed = False
            if self._aftermath is not None:
                self._aftermath()
            message = f"host crashed at {where} (now {self.at:g} s)"
            raise HostCrashedError(message)

    async def sleep(self, seconds: float) -> None:
        """Let background work finish, then advance logical time instead of waiting.

        The lease heartbeat waits for logical time that others advance: it only yields.

        Waiting is bounded to ``SETTLE_TURNS`` event-loop turns. A task that is waiting on
        time itself (another sleeper, or a controller that awaits one) cannot finish until
        this sleep returns, so waiting for every task unconditionally deadlocks as soon
        as two tasks wait on the clock. Work that needs real time to finish belongs on a
        ``tests.support.virtual_time.VirtualClock``, which has no such bound.
        """
        current = asyncio.current_task()
        if current is not None and current.get_name() == HEARTBEAT_TASK:
            await asyncio.sleep(0)
            return
        if current is not None and current.get_name() == WAIT_TASK:
            # The loop sleeps beside running requests and a real sleep would end when one
            # finishes: no logical time passes until they are done (they may need threads), and
            # none passes after, since the loop stops waiting then.
            running = [
                task
                for task in asyncio.all_tasks()
                if task.get_name().startswith("dispatch:") and not task.done()
            ]
            if running:
                await asyncio.wait(running)
                return
        self._crash_if_armed(f"a wait of {seconds:g} s")
        if self.limit is not None and self.at + seconds > self.limit:
            message = f"simulated time passed its limit of {self.limit:g} s (now {self.at:g} s)"
            raise ClockLimitError(message)
        if not self.settle_background:
            await asyncio.sleep(0)
        background = [
            task
            for task in asyncio.all_tasks()
            if self.settle_background and task is not current and task.get_name() != HEARTBEAT_TASK
        ]
        for _ in range(SETTLE_TURNS):
            if all(task.done() for task in background):
                break
            await asyncio.sleep(0)
        self.sleeps.append(seconds)
        self.at += seconds
