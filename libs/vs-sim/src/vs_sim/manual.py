"""A clock that moves only when a test says so."""

from __future__ import annotations

import asyncio
from threading import RLock


class ManualClock:
    """A thread-safe clock that moves only by ``advance`` and by its own sleeps.

    ``sleep`` advances the timeline by its duration and lets other ready tasks run, so a
    component whose sleeps are its only waits runs through hours of pacing at once. Each
    sleep moves time on its own: sleeps that overlap add up, where on a real or virtual
    clock they would overlap. A test that times concurrent sleepers uses ``VirtualClock``.
    """

    def __init__(self, start: float = 0.0) -> None:
        """Start the timeline at ``start`` seconds."""
        self._at = start
        self._lock = RLock()

    def now(self) -> float:
        """The current time."""
        with self._lock:
            return self._at

    def advance(self, seconds: float) -> None:
        """Move the timeline forward; time never runs backwards."""
        if seconds < 0:
            message = f"a clock cannot advance by {seconds:g} s"
            raise ValueError(message)
        with self._lock:
            self._at += seconds

    async def sleep(self, seconds: float) -> None:
        """Advance by ``seconds`` (none when not positive), then let other ready tasks run."""
        self.advance(max(0.0, seconds))
        await asyncio.sleep(0)
