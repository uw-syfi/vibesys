from __future__ import annotations

import asyncio

import pytest

from tests.support.virtual_time import VirtualTime
from vs_bench.runner import run
from vs_bench.schedule import closed_loop, duration_limited


class TestRun:
    @pytest.mark.asyncio
    async def test_collects_all_results(self) -> None:
        async def send(i: int) -> int:
            return i * 2

        result = await run(closed_loop(5), send, concurrency=5)
        values = [int(str(r)) for r in result.results]
        assert sorted(values) == [0, 2, 4, 6, 8]

    @pytest.mark.asyncio
    async def test_concurrency_respected(self) -> None:
        virtual_time = VirtualTime()
        max_concurrent = 0
        current = 0

        async def send(i: int) -> int:
            nonlocal max_concurrent, current
            current += 1
            max_concurrent = max(max_concurrent, current)
            await virtual_time.sleep(0.02)
            current -= 1
            return i

        result = await run(closed_loop(20), send, concurrency=4)
        assert len(result.results) == 20
        assert max_concurrent <= 4

    @pytest.mark.asyncio
    async def test_wall_clock_reflects_parallelism(self) -> None:
        virtual_time = VirtualTime()
        in_flight = 0
        all_in = asyncio.Event()

        async def send(i: int) -> int:
            nonlocal in_flight
            in_flight += 1
            if in_flight == 8:
                all_in.set()
            # Every send is in flight before any finishes: the eight run at once.
            await all_in.wait()
            await virtual_time.sleep(0.05)
            return i

        # The bound only guards against a hang if the sends were serialized.
        result = await asyncio.wait_for(
            run(closed_loop(8), send, concurrency=8, clock=virtual_time.now), timeout=60
        )
        assert result.wall_clock == pytest.approx(0.05)
        assert len(result.results) == 8

    @pytest.mark.asyncio
    async def test_serial_execution(self) -> None:
        virtual_time = VirtualTime()

        async def send(i: int) -> int:
            await virtual_time.sleep(0.02)
            return i

        result = await run(closed_loop(3), send, concurrency=1, clock=virtual_time.now)
        assert len(result.results) == 3
        assert result.wall_clock == pytest.approx(0.06)

    @pytest.mark.asyncio
    async def test_duration_limited_stops(self) -> None:
        virtual_time = VirtualTime()

        async def send(i: int) -> int:
            await virtual_time.sleep(0.03)
            return i

        result = await run(
            duration_limited(0.1, clock=virtual_time.now),
            send,
            concurrency=2,
            clock=virtual_time.now,
        )
        # The schedule stops admitting at 0.1 s; sends already admitted finish.
        assert 0.1 <= result.wall_clock < 0.1 + 0.03 * 2
        assert len(result.results) > 0

    @pytest.mark.asyncio
    async def test_empty_schedule(self) -> None:
        virtual_time = VirtualTime()

        async def send(i: int) -> int:
            return i

        result = await run(closed_loop(0), send, concurrency=1, clock=virtual_time.now)
        assert result.results == []
        assert result.wall_clock == 0

    @pytest.mark.asyncio
    async def test_exception_propagates(self) -> None:
        async def send(i: int) -> int:
            if i == 2:
                raise ValueError("boom")
            return i

        with pytest.raises(ValueError, match="boom"):
            await run(closed_loop(5), send, concurrency=5)
