"""The time sources one run is composed with."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_sim.api import SleepingClock, Threads


@dataclass(frozen=True)
class RunTiming:
    """The time sources one run is composed with.

    ``clock`` is the run's epoch timeline for the loop and its timestamps;
    ``threads`` is the blocking time (monotonic reads, sleeps, locks) that code on
    worker threads, such as an agent turn, waits on. A production entrypoint builds
    both from the real implementations; a simulated run builds them from one simulation.
    ``control_poll_seconds`` is how often an idle loop reads the operator's controls (pause
    and steer; a stop never waits for it). Production reads them every second; a simulated
    run of many hours may read them less often, so its loop takes fewer idle steps.
    """

    clock: SleepingClock
    threads: Threads
    control_poll_seconds: float = 1.0

    def __post_init__(self) -> None:
        """Reject a control poll that never comes."""
        if self.control_poll_seconds <= 0:
            message = "control_poll_seconds must be positive"
            raise ValueError(message)
