"""Async generators that control request dispatch timing."""

from __future__ import annotations

import asyncio
import math
import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

#: Waits the given number of seconds; a test passes a virtual one.
type Sleep = Callable[[float], Awaitable[None]]


async def poisson(
    n: int, rate: float, *, seed: int = 42, sleep: Sleep = asyncio.sleep
) -> AsyncIterator[int]:
    """Yield *n* indices with Poisson-distributed inter-arrival delays.

    ``rate`` is the mean arrival rate in requests per second.
    """
    # lint-waiver: LW-011002 [S311]; The seeded generator controls reproducible benchmark timing and is not used for security.
    rng = random.Random(seed)  # noqa: S311
    for i in range(n):
        yield i
        if i < n - 1:
            delay = -math.log(1.0 - rng.random()) / rate
            await sleep(delay)


async def constant(n: int, rate: float, *, sleep: Sleep = asyncio.sleep) -> AsyncIterator[int]:
    """Yield *n* indices at a fixed rate (requests per second)."""
    interval = 1.0 / rate
    for i in range(n):
        yield i
        if i < n - 1:
            await sleep(interval)


async def closed_loop(n: int) -> AsyncIterator[int]:
    """Yield *n* indices with no inter-arrival delay.

    When paired with ``run(concurrency=C)``, this produces closed-loop
    behavior: the semaphore naturally paces dispatch to server capacity.
    """
    for i in range(n):
        yield i


async def duration_limited(
    duration: float, *, clock: Callable[[], float] = time.perf_counter
) -> AsyncIterator[int]:
    """Yield indices continuously until *duration* seconds have elapsed.

    No inter-arrival delay; use with ``run(concurrency=C)`` for closed-loop
    load that stops after a wall-clock budget.
    """
    start = clock()
    i = 0
    while clock() - start < duration:
        yield i
        i += 1
