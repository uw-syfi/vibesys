"""``clock_from`` moves the clock of the virtual loop the code runs on."""

from __future__ import annotations

from vs_sim.api.testing import VirtualClock, clock_from, run_virtual


def test_clock_from_moves_the_loops_clock() -> None:
    clock = VirtualClock()

    async def body() -> float:
        return clock_from(42.0).now()

    assert run_virtual(clock, body()) == 42.0
    assert clock.at == 42.0
