"""Throwaway: an order-dependent test that must fail its own PR under seed exploration."""

import asyncio


async def test_two_tasks_assume_a_fixed_wake_order():
    woke: list[str] = []

    async def claim(name: str) -> None:
        await asyncio.sleep(1)
        woke.append(name)

    await asyncio.gather(claim("a"), claim("b"), claim("c"))
    assert woke == ["a", "b", "c"]
