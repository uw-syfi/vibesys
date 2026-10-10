"""A virtual run clock that can kill the simulated host at its next clock call."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from vs_runtime.api.core import HEARTBEAT_TASK
from vs_sim.api.testing import VirtualClock, current_virtual_clock

if TYPE_CHECKING:
    from collections.abc import Callable


class HostCrashedError(RuntimeError):
    """The simulated host process died (see ``CrashableClock.crash_on_next_clock_call``)."""


def _in_heartbeat() -> bool:
    """Whether the caller is the lease heartbeat task (false outside an event loop)."""
    try:
        current = asyncio.current_task()
    except RuntimeError:
        return False
    return current is not None and current.get_name() == HEARTBEAT_TASK


class CrashableClock(VirtualClock):
    """The virtual clock, plus a scripted host death.

    ``crash_on_next_clock_call`` arms a crash for the loop's next clock read or wait, after
    whatever the host committed before it. A scenario arms it from inside a scripted agent
    turn to choose the commit the crash follows.
    """

    def __init__(self, at: float = 0.0, *, limit: float | None = None) -> None:
        """Start the timeline at ``at`` seconds."""
        super().__init__(at, limit=limit)
        self._armed = False
        self._aftermath: Callable[[], None] | None = None

    def crash_on_next_clock_call(self, *, aftermath: Callable[[], None] | None = None) -> None:
        """Kill the simulated host at the loop's next clock read or wait.

        The run raises :class:`HostCrashedError` from that call, so nothing the host would
        have done afterwards happens. ``aftermath`` runs at the moment of death, to leave
        behind what a dying process leaves (for example an unreadable file).
        """
        self._armed = True
        self._aftermath = aftermath

    def now(self) -> float:
        """The current virtual time, or the host's death if one is armed."""
        self._crash_if_armed("a clock read")
        return super().now()

    async def sleep(self, seconds: float) -> None:
        """Wait on the virtual timeline, or die first if a crash is armed."""
        self._crash_if_armed(f"a wait of {seconds:g} s")
        await super().sleep(seconds)

    def _crash_if_armed(self, where: str) -> None:
        # The lease heartbeat runs beside an in-flight agent turn: a crash it took would
        # land before the turn's reply is committed, not at the loop's next step.
        if _in_heartbeat() or not self._armed:
            return
        self._armed = False
        if self._aftermath is not None:
            self._aftermath()
        message = f"host crashed at {where} (now {self.at:g} s)"
        raise HostCrashedError(message)


def clock_from(start: float) -> VirtualClock:
    """The clock of the virtual loop this code runs on, moved to ``start``.

    For a test body that builds its world inside the simulation (``sim.clock`` is the same
    clock): call it before anything sleeps, because moving the clock under a pending timer
    would change when that timer is due.
    """
    clock = current_virtual_clock()
    clock.at = start
    return clock


class ProbedClock:
    """The loop's virtual clock, recording the waits the run loop itself makes.

    ``sleeps`` lists the durations of every wait except the lease heartbeat's, which renews
    beside the loop and is not what a pacing test counts. ``at`` reads and moves the shared
    timeline, for a scenario that lets time pass while a dispatch is in flight.
    """

    def __init__(self, inner: VirtualClock) -> None:
        """Wrap ``inner``, the clock of the virtual loop the test runs on."""
        self._inner = inner
        self.sleeps: list[float] = []

    @property
    def at(self) -> float:
        """The shared virtual time."""
        return self._inner.at

    @at.setter
    def at(self, value: float) -> None:
        self._inner.at = value

    def now(self) -> float:
        """The current virtual time."""
        return self._inner.now()

    async def sleep(self, seconds: float) -> None:
        """Wait on the shared timeline, recording the wait unless it is the heartbeat's."""
        if not _in_heartbeat():
            self.sleeps.append(seconds)
        await self._inner.sleep(seconds)

    async def pass_time(self, seconds: float) -> None:
        """Let ``seconds`` of the shared timeline pass for the scenario, not as a run-loop wait."""
        await self._inner.sleep(seconds)
