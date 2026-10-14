"""Virtual seconds for async code that takes a clock and a sleep, such as vs-bench's runner."""

from __future__ import annotations

from vs_sim.api import yield_now


class VirtualTime:
    """A clock and an async sleep that advance only virtual seconds.

    A sleep from ``t`` for ``d`` seconds yields to the event loop once, then
    moves the clock to ``max(now, t + d)``. Sleeps that overlap therefore cost
    their longest one, not their sum, which is what concurrency buys. The model
    is exact when sleepers wake in the order they fell asleep, as equal sleeps
    started together and serial sleeps do.
    """

    def __init__(self) -> None:
        """Start at virtual second zero."""
        self.seconds = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        """The current virtual second."""
        return self.seconds

    async def sleep(self, delay: float) -> None:
        """Wait ``delay`` virtual seconds."""
        self.sleeps.append(delay)
        wake = self.seconds + delay
        await yield_now()
        self.seconds = max(self.seconds, wake)


__all__ = ["VirtualTime"]
