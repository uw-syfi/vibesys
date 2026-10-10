"""A record of what a simulated run did, comparable across two runs of the same test."""

from __future__ import annotations

from dataclasses import dataclass, field

type TraceEvent = tuple[str, ...]
"""``("advance", from, to)``, ``("step", task)`` or ``("io", count)``, as strings."""


@dataclass
class EventTrace:
    """The events of one virtual run, in order.

    A run that depends only on its seed and the virtual clock produces the same trace
    every time. A hidden wall-clock read, a real thread or socket, or an unseeded random
    draw changes which tasks run when, and shows up as the first differing event.
    """

    events: list[TraceEvent] = field(default_factory=list)

    def advance(self, start: float, end: float) -> None:
        """Record the clock jumping from ``start`` to ``end``."""
        self.events.append(("advance", repr(start), repr(end)))

    def step(self, task: str) -> None:
        """Record one scheduling step of ``task`` (a run of it up to its next wait)."""
        self.events.append(("step", task))

    def io(self, ready: int) -> None:
        """Record ``ready`` file descriptors becoming ready: input from outside the simulation."""
        self.events.append(("io", str(ready)))

    def first_difference(self, other: EventTrace) -> str | None:
        """Describe where this trace and ``other`` first differ, or ``None`` when identical."""
        for index, (mine, theirs) in enumerate(zip(self.events, other.events, strict=False)):
            if mine != theirs:
                return f"event {index}: {mine} != {theirs}"
        if len(self.events) != len(other.events):
            return f"one trace ends after {min(len(self.events), len(other.events))} events"
        return None
