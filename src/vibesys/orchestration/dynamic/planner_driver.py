"""Planner mode as a driver over ``HostCore``: one structured planning turn per refill.

A refill is due after the loop starts and after any worker finishes. The
driver plans only when a refill is due, the core may start work, and a slot
is free within the budget; it finishes the search when no refill is due and
nothing runs or waits. Planning itself is injected, so this policy is
testable without agents.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.agent_loop import DriverStep
from vibesys.orchestration.dynamic.control import WorkerFinished, WorkItem

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vibesys.orchestration.dynamic.control import HostCore, HostEvent


@dataclass(slots=True)
class PlannerDriver[P]:
    """Drive the core with a one-shot planner called when a slot frees.

    ``plan(capacity, in_flight)`` returns at most ``capacity`` new items; it
    may return none, which leaves the free slots idle until the next refill.
    ``checkpoint`` lands an operator stop by raising.
    """

    plan: Callable[[int, frozenset[str]], Awaitable[tuple[WorkItem[P], ...]]]
    land_stop: Callable[[], Awaitable[None]]
    _refill: bool = True

    async def checkpoint(self) -> None:
        """Land a pending stop before planning starts work."""
        await self.land_stop()

    def observe(self, event: HostEvent) -> None:
        """A finished worker frees a slot or changes what the planner should see."""
        if isinstance(event, WorkerFinished):
            self._refill = True

    def next_step(self, core: HostCore[P]) -> DriverStep:
        """Plan once per refill while a slot is free; finish when nothing remains."""
        refill, self._refill = self._refill, False
        if refill and core.free_capacity > 0:
            return DriverStep.TURN
        if not core.running and not core.queued:
            return DriverStep.FINISH
        return DriverStep.WAIT

    async def turn(self, core: HostCore[P]) -> tuple[WorkItem[P], ...]:
        """Plan for the core's free capacity, shown the ids already in flight."""
        return await self.plan(core.free_capacity, core.running)
