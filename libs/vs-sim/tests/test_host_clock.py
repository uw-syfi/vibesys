"""A crashable clock kills the host at its next clock call; a probed clock records the waits made."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api.testing import (
    CrashableClock,
    HostCrashedError,
    ProbedClock,
    VirtualClock,
    clock_from,
    run_virtual,
)

DURATIONS = st.lists(st.floats(min_value=0.01, max_value=500.0), min_size=1, max_size=8)


@given(start=st.floats(min_value=0.0, max_value=1e6), reads=st.integers(0, 5))
def test_an_unarmed_clock_behaves_as_the_virtual_clock(start: float, reads: int) -> None:
    clock = CrashableClock(start)
    assert [clock.now() for _ in range(reads)] == [start] * reads


@given(armed_after=st.integers(0, 4), use_sleep=st.booleans())
def test_an_armed_crash_fires_at_the_next_call_exactly_once(
    armed_after: int, *, use_sleep: bool
) -> None:
    clock = CrashableClock()
    deaths: list[str] = []

    async def body() -> int:
        for _ in range(armed_after):
            await clock.sleep(1.0)
        clock.crash_on_next_clock_call(aftermath=lambda: deaths.append("left behind"))
        with pytest.raises(HostCrashedError):
            await clock.sleep(1.0) if use_sleep else clock.now()
        # Disarmed: the restarted host runs on.
        await clock.sleep(1.0)
        return len(deaths)

    assert run_virtual(clock, body()) == 1
    assert deaths == ["left behind"]


def test_the_exempt_task_does_not_take_the_crash_and_the_crash_stays_armed() -> None:
    clock = CrashableClock(exempt_task="heartbeat")

    async def exempt() -> None:
        await clock.sleep(1.0)
        clock.now()

    async def body() -> None:
        clock.crash_on_next_clock_call()
        await asyncio.create_task(exempt(), name="heartbeat")
        with pytest.raises(HostCrashedError):
            clock.now()

    run_virtual(clock, body())


@given(durations=DURATIONS)
def test_a_probed_clock_records_every_wait_but_the_exempt_tasks(durations: list[float]) -> None:
    inner = VirtualClock()
    probed = ProbedClock(inner, exempt_task="heartbeat")

    async def renew() -> None:
        await probed.sleep(0.5)

    async def body() -> None:
        await asyncio.create_task(renew(), name="heartbeat")
        for duration in durations:
            await probed.sleep(duration)
        await probed.pass_time(3.0)

    run_virtual(inner, body())
    assert probed.sleeps == durations


def test_clock_from_moves_the_loops_clock() -> None:
    clock = VirtualClock()

    async def body() -> float:
        return clock_from(42.0).now()

    assert run_virtual(clock, body()) == 42.0
    assert clock.at == 42.0
