"""The fake run clock cannot deadlock its sleepers (retired with the clock migration)."""

from __future__ import annotations

import asyncio

from hypothesis import given
from hypothesis import strategies as st
from tests.support.fake_run_clock import FakeRunClock

from vs_sim.api.testing import VirtualClock, run_virtual


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
