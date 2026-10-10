"""Time and sleeping, split into the roles callers need.

``Clock.now`` is seconds on one timeline that never runs backwards. Which timeline
(seconds since the epoch, or an arbitrary origin) is the implementation's choice, so a
reader compares ``now`` only with values from the same clock. Code that compares with a
persisted timestamp or another host needs :class:`SystemClock`; code that measures a
duration or a deadline takes any clock.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    """A time source."""

    def now(self) -> float:
        """Seconds on this clock's timeline; never less than an earlier reading."""
        ...


class Sleeper(Protocol):
    """Something that waits for a duration."""

    async def sleep(self, seconds: float) -> None:
        """Wait about ``seconds`` on this sleeper's timeline; other tasks run meanwhile."""
        ...


class SleepingClock(Clock, Sleeper, Protocol):
    """A clock that can also wait: the run loop reads ``now`` and sleeps on one timeline."""


class SystemClock:
    """Seconds since the epoch, with real sleeping: the timeline every host shares."""

    def now(self) -> float:
        """Seconds since the epoch."""
        return time.time()

    async def sleep(self, seconds: float) -> None:
        """Sleep on the event loop."""
        await asyncio.sleep(seconds)


class MonotonicClock:
    """Seconds on the operating system's monotonic timeline, with real sleeping."""

    def now(self) -> float:
        """Monotonic seconds; only differences are meaningful."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Sleep on the event loop."""
        await asyncio.sleep(seconds)
