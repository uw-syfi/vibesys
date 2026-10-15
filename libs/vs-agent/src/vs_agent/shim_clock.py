"""Adapt VibeSys's blocking time to the clock interface agentshim waits on.

agentshim measures turn timeouts, retry delays and stream polls through its own
``Clock``: a monotonic reading plus a wait that a ``StopSignal`` can cut short.
``ThreadsClock`` provides that interface over a :class:`~vs_sim.api.Threads`,
so a session's time is the run's time: real under ``OsThreads``, virtual under
``SimThreads``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentshim import StopSignal

    from vs_sim.api import Threads

STOP_POLL_S = 0.05
"""How often a wait looks at its stop signal; also the longest a stop goes unnoticed."""


class ThreadsClock:
    """agentshim's ``Clock`` over ``threads``' monotonic time and blocking sleep."""

    def __init__(self, threads: Threads) -> None:
        """Read and wait on ``threads``."""
        self._threads = threads

    def monotonic(self) -> float:
        """Seconds on ``threads``' monotonic clock."""
        return self._threads.now()

    def wait(self, seconds: float, stop: StopSignal | None = None) -> bool:
        """Wait ``seconds``, or until ``stop`` is set; ``True`` if it was set.

        A stop signal is a plain flag that cannot wake a sleeping thread, so
        the wait sleeps in slices of ``STOP_POLL_S`` and checks the flag
        between them. A wait without a signal sleeps once.
        """
        if stop is None:
            self._threads.sleep(seconds)
            return False
        deadline = self._threads.now() + max(seconds, 0.0)
        while not stop.is_set():
            remaining = deadline - self._threads.now()
            if remaining <= 0:
                return False
            self._threads.sleep(min(remaining, STOP_POLL_S))
        return True
