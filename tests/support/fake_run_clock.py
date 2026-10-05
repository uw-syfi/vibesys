"""A run clock that never waits: ``sleep`` advances a counter and records the request."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from vs_runtime.api.core import HEARTBEAT_TASK

#: Event-loop turns ``sleep`` lets background tasks run before logical time moves.
SETTLE_TURNS = 200


@dataclass
class FakeRunClock:
    """Logical seconds on the shared timeline. ``sleep`` moves time forward by its argument.

    A real sleep lets the event loop run the executors' background work (a Slurm submission
    runs as a task beside the loop). The fake keeps that: while logical time passes, every
    other task the loop is running finishes first, so a job submitted before a sleep has
    been accepted by the scheduler after it, without any wall-clock wait.
    """

    at: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> float:
        """The current logical time."""
        return self.at

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
        background = [
            task
            for task in asyncio.all_tasks()
            if task is not current and task.get_name() != HEARTBEAT_TASK
        ]
        for _ in range(SETTLE_TURNS):
            if all(task.done() for task in background):
                break
            await asyncio.sleep(0)
        self.sleeps.append(seconds)
        self.at += seconds
