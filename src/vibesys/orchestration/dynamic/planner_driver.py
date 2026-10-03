"""Planner mode as a driver over ``HostCore``: one structured planning turn per refill.

The driver plans whenever the core wants a turn: a slot is free within the
budget and fewer than ``turn_attempts`` turns in a row (since the last
finished worker) faulted or left a slot free. It finishes the search when no
turn is due and nothing runs or waits. Planning itself is injected, so this
policy is testable without agents.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.agent_loop import DriverStep

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vibesys.orchestration.dynamic.control import HostCore, HostEvent, WorkItem


@dataclass(slots=True)
class PlannerDriver[P]:
    """Drive the core with a one-shot planner called when a slot frees.

    ``plan(capacity, in_flight)`` returns at most ``capacity`` new items; it
    may return none, which leaves the free slots idle until the next refill.
    ``checkpoint`` lands an operator stop by raising.
    """

    plan: Callable[[int, frozenset[str]], Awaitable[tuple[WorkItem[P], ...]]]
    land_stop: Callable[[], Awaitable[None]]

    async def checkpoint(self) -> None:
        """Land a pending stop before planning starts work."""
        await self.land_stop()

    def observe(self, event: HostEvent) -> None:
        """Nothing to learn: the core decides when a turn is due."""
        del event

    def next_step(self, core: HostCore[P]) -> DriverStep:
        """Plan while the core wants a turn; finish when nothing remains."""
        if core.wants_turn:
            return DriverStep.TURN
        if not core.running and not core.queued:
            return DriverStep.FINISH
        return DriverStep.WAIT

    async def turn(self, core: HostCore[P]) -> tuple[WorkItem[P], ...]:
        """Plan for the core's free capacity, shown the ids already in flight."""
        return await self.plan(core.free_capacity, core.running)
