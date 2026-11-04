"""Hand the event loop to other ready tasks."""

from __future__ import annotations

import asyncio


async def yield_now() -> None:
    """Let other ready tasks run without waiting on any clock.

    One call gives every task that is ready right now one turn before the caller
    resumes; it never advances real or virtual time. Code that waits for a state
    waits on that state with a bound instead of yielding in a loop.
    """
    await asyncio.sleep(0)
