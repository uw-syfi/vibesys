"""Test clocks for a run loop: the generic virtual-time clocks, bound to the lease heartbeat."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime._core_run import HEARTBEAT_TASK
from vs_sim.api.testing import CrashableClock, ProbedClock

if TYPE_CHECKING:
    from vs_sim.api.testing import VirtualClock


class CrashableRunClock(CrashableClock):
    """The virtual clock with a scripted host death that the lease heartbeat never takes.

    The heartbeat renews beside an in-flight agent turn; a crash it took would land before
    the turn's reply is committed, not at the run loop's next step.
    """

    def __init__(self, at: float = 0.0, *, limit: float | None = None) -> None:
        """Start the timeline at ``at`` seconds."""
        super().__init__(at, limit=limit, exempt_task=HEARTBEAT_TASK)


class ProbedRunClock(ProbedClock):
    """The loop's virtual clock, recording the waits the run loop itself makes.

    The lease heartbeat's waits are not recorded: it renews beside the loop and is not what a
    pacing test counts.
    """

    def __init__(self, inner: VirtualClock) -> None:
        """Wrap ``inner``, the clock of the virtual loop the test runs on."""
        super().__init__(inner, exempt_task=HEARTBEAT_TASK)
