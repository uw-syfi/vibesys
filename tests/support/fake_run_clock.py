"""A run clock that never waits: ``sleep`` advances a counter and records the request."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


class ClockLimitError(RuntimeError):
    """A simulated run waited past ``FakeRunClock.limit``: it is stuck, not slow."""


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

    def now(self) -> float:
        """The current logical time."""
        return self.at

    async def sleep(self, seconds: float) -> None:
        """Let background work finish, then advance logical time instead of waiting."""
        if self.limit is not None and self.at + seconds > self.limit:
            message = f"simulated time passed its limit of {self.limit:g} s (now {self.at:g} s)"
            raise ClockLimitError(message)
        if not self.settle_background:
            await asyncio.sleep(0)
        background = [
            task
            for task in asyncio.all_tasks()
            if self.settle_background and task is not asyncio.current_task()
        ]
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        self.sleeps.append(seconds)
        self.at += seconds
