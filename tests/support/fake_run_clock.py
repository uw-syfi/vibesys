"""A run clock that never sleeps: ``sleep`` advances a counter and records the request."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeRunClock:
    """Logical seconds on the shared timeline. ``sleep`` moves time forward by its argument."""

    at: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> float:
        """The current logical time."""
        return self.at

    async def sleep(self, seconds: float) -> None:
        """Advance logical time instead of waiting."""
        self.sleeps.append(seconds)
        self.at += seconds
