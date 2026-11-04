"""``yield_now`` gives every ready task a turn and moves no clock."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import yield_now
from vs_sim.api.testing import current_virtual_clock, yield_until


async def _record(order: list[int], index: int) -> None:
    order.append(index)


@given(st.integers(min_value=0, max_value=8))
def test_every_ready_task_runs_before_the_caller_resumes(ready: int) -> None:
    async def scenario() -> list[int]:
        order: list[int] = []
        tasks = [asyncio.ensure_future(_record(order, index)) for index in range(ready)]
        await yield_now()
        order.append(-1)
        await asyncio.gather(*tasks)
        return order

    assert asyncio.run(scenario()) == [*range(ready), -1]


async def test_a_yield_moves_no_virtual_time() -> None:
    clock = current_virtual_clock()
    before = clock.now()

    await yield_now()

    assert clock.now() == before


@given(st.integers(min_value=0, max_value=20), st.integers(min_value=1, max_value=20))
def test_yield_until_returns_the_first_satisfying_look_or_fails_at_its_bound(
    needed: int, turns: int
) -> None:
    looks: list[int] = []

    async def observe() -> int:
        looks.append(len(looks))
        return looks[-1]

    async def scenario() -> int:
        return await yield_until(observe, lambda seen: seen >= needed, "the count", turns=turns)

    if needed < turns:
        assert asyncio.run(scenario()) == needed
    else:
        with pytest.raises(AssertionError, match="the count did not hold"):
            asyncio.run(scenario())
        assert len(looks) == turns
