"""Async generators that control request dispatch timing."""

from __future__ import annotations

import asyncio
import math
import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def poisson(n: int, rate: float, *, seed: int = 42) -> AsyncIterator[int]:
    """Yield *n* indices with Poisson-distributed inter-arrival delays.

    ``rate`` is the mean arrival rate in requests per second.
    """
    rng = random.Random(seed)
    for i in range(n):
        yield i
        if i < n - 1:
            delay = -math.log(1.0 - rng.random()) / rate
            await asyncio.sleep(delay)


async def constant(n: int, rate: float) -> AsyncIterator[int]:
    """Yield *n* indices at a fixed rate (requests per second)."""
    interval = 1.0 / rate
    for i in range(n):
        yield i
        if i < n - 1:
            await asyncio.sleep(interval)


async def closed_loop(n: int) -> AsyncIterator[int]:
    """Yield *n* indices with no inter-arrival delay.

    When paired with ``run(concurrency=C)``, this produces closed-loop
    behavior: the semaphore naturally paces dispatch to server capacity.
    """
    for i in range(n):
        yield i


async def duration_limited(duration: float) -> AsyncIterator[int]:
    """Yield indices continuously until *duration* seconds have elapsed.

    No inter-arrival delay; use with ``run(concurrency=C)`` for closed-loop
    load that stops after a wall-clock budget.
    """
    start = time.perf_counter()
    i = 0
    while time.perf_counter() - start < duration:
        yield i
        i += 1
