from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.support.virtual_time import VirtualTime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from vs_bench.schedule import closed_loop, constant, duration_limited, poisson


async def _collect(ait: AsyncIterator[int], max_items: int = 10000) -> list[int]:
    items: list[int] = []
    async for i in ait:
        items.append(i)
        if len(items) >= max_items:
            break
    return items


class TestClosedLoop:
    @pytest.mark.asyncio
    async def test_yields_n_indices(self) -> None:
        result = await _collect(closed_loop(5))
        assert result == [0, 1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_zero_yields_nothing(self) -> None:
        result = await _collect(closed_loop(0))
        assert result == []


class TestConstant:
    @pytest.mark.asyncio
    async def test_yields_correct_count(self) -> None:
        result = await _collect(constant(3, rate=100.0))
        assert result == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_waits_one_interval_between_indices(self) -> None:
        virtual_time = VirtualTime()
        await _collect(constant(3, rate=20.0, sleep=virtual_time.sleep))
        assert virtual_time.sleeps == pytest.approx([0.05, 0.05])


class TestPoisson:
    @pytest.mark.asyncio
    async def test_yields_correct_count(self) -> None:
        virtual_time = VirtualTime()
        result = await _collect(poisson(5, rate=1000.0, seed=1, sleep=virtual_time.sleep))
        assert result == [0, 1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_deterministic_with_seed(self) -> None:
        virtual_time = VirtualTime()
        await _collect(poisson(10, rate=100.0, seed=99, sleep=virtual_time.sleep))
        await _collect(poisson(10, rate=100.0, seed=99, sleep=virtual_time.sleep))
        first, second = virtual_time.sleeps[:9], virtual_time.sleeps[9:]
        assert first == second
        assert len(first) == 9

    @pytest.mark.asyncio
    async def test_total_time_reasonable(self) -> None:
        virtual_time = VirtualTime()
        await _collect(poisson(10, rate=50.0, seed=7, sleep=virtual_time.sleep))
        # Nine gaps at a mean of 20 ms each.
        assert 0.05 < virtual_time.now() < 1.0


class TestDurationLimited:
    @pytest.mark.asyncio
    async def test_stops_after_duration(self) -> None:
        virtual_time = VirtualTime()
        indices = duration_limited(0.1, clock=virtual_time.now)
        result: list[int] = []
        async for i in indices:
            result.append(i)
            await virtual_time.sleep(0.03)
        # Yields at 0, 0.03, 0.06 and 0.09 virtual seconds, then stops.
        assert result == [0, 1, 2, 3]

    @pytest.mark.asyncio
    async def test_indices_start_at_zero(self) -> None:
        virtual_time = VirtualTime()
        indices = duration_limited(0.05, clock=virtual_time.now)
        result: list[int] = []
        async for i in indices:
            result.append(i)
            await virtual_time.sleep(0.01)
        assert result[0] == 0
        assert result == list(range(len(result)))

    @pytest.mark.asyncio
    async def test_zero_duration_yields_nothing(self) -> None:
        virtual_time = VirtualTime()
        result = await _collect(duration_limited(0.0, clock=virtual_time.now))
        assert result == []
