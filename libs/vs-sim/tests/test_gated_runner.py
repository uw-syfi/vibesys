"""A gated runner keeps a call in flight until the test releases it."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api.testing import GatedBlockingRunner, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Coroutine


def run[T](main: Coroutine[object, object, T]) -> T:
    return run_virtual(VirtualClock(), main)


@given(count=st.integers(min_value=1, max_value=8))
def test_held_calls_park_without_running_and_all_run_once_released(count: int) -> None:
    runner = GatedBlockingRunner(held=True)
    ran: list[int] = []

    async def main() -> list[int]:
        tasks = [asyncio.ensure_future(runner.run(ran.append, n)) for n in range(count)]
        await runner.wait_in_flight(count, *tasks)
        assert ran == []
        assert runner.in_flight == count
        runner.release()
        await asyncio.gather(*tasks)
        return ran

    assert run(main()) == list(range(count))
    assert runner.in_flight == 0
    assert runner.max_in_flight == count


def test_calls_that_never_overlap_report_one_in_flight() -> None:
    runner = GatedBlockingRunner()

    async def main() -> None:
        for _ in range(3):
            await runner.run(int)

    run(main())
    assert (runner.calls, runner.max_in_flight) == (3, 1)


def test_cancelling_a_parked_caller_abandons_its_call() -> None:
    runner = GatedBlockingRunner(held=True)
    ran: list[int] = []

    async def main() -> None:
        task = asyncio.ensure_future(runner.run(ran.append, 1))
        await runner.wait_in_flight(1, task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        runner.release()

    run(main())
    assert ran == []
    assert runner.in_flight == 0


def test_waiting_for_a_call_that_never_comes_fails_with_the_operation_outcome() -> None:
    runner = GatedBlockingRunner(held=True)

    async def finishes_without_calling() -> None:
        return None

    async def main() -> None:
        task = asyncio.ensure_future(finishes_without_calling())
        await runner.wait_in_flight(1, task)

    with pytest.raises(AssertionError, match="finished without the arrival"):
        run(main())
