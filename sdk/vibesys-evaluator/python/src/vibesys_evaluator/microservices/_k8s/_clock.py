"""The clock the deploy layer waits with: `SystemClock`, or `FakeClock` in tests."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Unix seconds (namespace ages)."""
        ...

    def monotonic(self) -> float:
        """Seconds for deadlines."""
        ...

    def sleep(self, secs: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, secs: float) -> None:
        time.sleep(secs)


class FakeClock:
    """Virtual time: sleep advances it at once."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._now

    def sleep(self, secs: float) -> None:
        self._now += max(secs, 0.0)
