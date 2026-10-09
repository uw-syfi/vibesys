"""The virtual clock shares one timeline among all waiters, and the fake run clock cannot deadlock."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.fake_run_clock import FakeRunClock
from tests.support.virtual_time import VirtualClock, VirtualDeadlockError, run_virtual


@given(st.lists(st.floats(min_value=0.01, max_value=500.0), min_size=1, max_size=8))
def test_concurrent_sleeps_overlap_and_wake_in_due_order(durations: list[float]) -> None:
    clock = VirtualClock(1.0)
    woke: list[tuple[float, float]] = []

    async def sleeper(seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append((seconds, clock.now()))

    async def main() -> None:
        await asyncio.gather(*(sleeper(seconds) for seconds in durations))

    run_virtual(clock, main())
    # Sleeps overlap: the run lasts as long as the longest one, not the sum.
    assert clock.now() == pytest.approx(1.0 + max(durations), abs=1e-6)
    assert [seconds for seconds, _ in woke] == sorted(durations)
    assert all(at == pytest.approx(1.0 + seconds, abs=1e-6) for seconds, at in woke)


def test_a_run_nothing_can_wake_fails_instead_of_hanging() -> None:
    async def main() -> None:
        await asyncio.Event().wait()

    with pytest.raises(VirtualDeadlockError):
        run_virtual(VirtualClock(), main())


@given(st.lists(st.floats(min_value=0.5, max_value=50.0), min_size=2, max_size=5))
def test_fake_run_clock_sleepers_do_not_wait_for_each_other(durations: list[float]) -> None:
    """Regression: ``sleep`` gathered every other task, sleepers included, so two tasks
    sleeping on the same clock waited for each other forever. Under the virtual loop that
    deadlock is an error rather than a hang.
    """
    clock = FakeRunClock(at=1.0)

    async def main() -> None:
        await asyncio.gather(*(clock.sleep(seconds) for seconds in durations))

    run_virtual(VirtualClock(), main())
    assert sorted(clock.sleeps) == sorted(durations)
