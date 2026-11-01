"""The error of a simulated host that died, and a helper that moves the loop's virtual clock."""

from __future__ import annotations

from vs_sim.virtual import VirtualClock, current_virtual_clock


class HostCrashedError(RuntimeError):
    """The simulated host process died."""


def clock_from(start: float) -> VirtualClock:
    """The clock of the virtual loop this code runs on, moved to ``start``.

    For a test body that builds its world inside the simulation (``sim.clock`` is the same
    clock): call it before anything sleeps, because moving the clock under a pending timer
    would change when that timer is due.
    """
    clock = current_virtual_clock()
    clock.at = start
    return clock
