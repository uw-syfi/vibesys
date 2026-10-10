"""A clock that moves only when a test says so."""

from __future__ import annotations

from threading import RLock


class ManualClock:
    """A thread-safe clock for code that reads time but never sleeps on it."""

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
