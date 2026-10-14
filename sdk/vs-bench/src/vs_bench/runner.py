"""Dispatch loop: pair a schedule with a send function under concurrency control."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable


@dataclass
class RunResult:
    """Results from a benchmark run."""

    results: list[object]
    wall_clock: float


async def run(
    schedule: AsyncIterator[int],
    send: Callable[[int], Awaitable[object]],
    *,
    concurrency: int = 1,
    clock: Callable[[], float] = time.perf_counter,
) -> RunResult:
    """Dispatch ``send(i)`` for each index from *schedule*, bounded by a semaphore.

    The semaphore creates backpressure: the schedule is only advanced when a
    concurrency slot is free. This gives closed-loop behavior when the schedule
    has no internal delays (e.g. ``duration_limited``, ``closed_loop``).

    For open-loop schedules (``poisson``, ``constant``), set *concurrency*
    high enough that it never blocks, letting the schedule control pacing.

    ``clock`` reads the seconds ``wall_clock`` is measured in; a test passes a
    virtual one.
    """
    sem = asyncio.Semaphore(concurrency)
    tasks: list[asyncio.Task[object]] = []
    start = clock()

    async def _wrap(i: int) -> object:
        try:
            return await send(i)
        finally:
            sem.release()

    async for i in schedule:
        await sem.acquire()
        tasks.append(asyncio.create_task(_wrap(i)))

    results: list[Any] = await asyncio.gather(*tasks)
    return RunResult(results, clock() - start)
