"""A shielded runner returns a cancelled caller's cancel only once the function has ended."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import ShieldedBlockingRunner, yield_now
from vs_sim.api.testing import DetachedBlockingRunner, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Coroutine


def run[T](main: Coroutine[object, object, T]) -> T:
    return run_virtual(VirtualClock(), main)


@given(extra_cancels=st.integers(min_value=0, max_value=5))
def test_a_cancelled_caller_is_released_only_after_the_function_ended(extra_cancels: int) -> None:
    thread = DetachedBlockingRunner(held=True)
    ended: list[str] = []

    async def main() -> None:
        call = asyncio.ensure_future(ShieldedBlockingRunner(thread).run(ended.append, "done"))
        await thread.wait_in_flight(1, call)
        for _ in range(1 + extra_cancels):
            call.cancel()
            await yield_now()
        assert not call.done(), "the call returned while its function was still in flight"
        thread.release()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert ended == ["done"]
        assert thread.in_flight == 0

    run(main())


def test_without_the_shield_a_cancelled_caller_returns_while_the_function_is_in_flight() -> None:
    """The failure the shield is for, shown on the Fake that models a thread."""
    thread = DetachedBlockingRunner(held=True)

    async def main() -> None:
        call = asyncio.ensure_future(thread.run(lambda: None))
        await thread.wait_in_flight(1, call)
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        assert call.done()
        assert thread.in_flight == 1
        thread.release()
        await yield_now()

    run(main())


def test_an_uncancelled_call_returns_the_functions_value_and_raises_its_error() -> None:
    runner = ShieldedBlockingRunner(DetachedBlockingRunner())

    def boom() -> None:
        message = "its own error"
        raise ValueError(message)

    async def main() -> None:
        assert await runner.run(lambda a, b: a + b, 2, b=3) == 5
        with pytest.raises(ValueError, match="its own error"):
            await runner.run(boom)

    run(main())


def test_a_function_that_fails_after_the_cancel_still_ends_in_the_cancel() -> None:
    thread = DetachedBlockingRunner(held=True)

    def boom() -> None:
        message = "late failure"
        raise ValueError(message)

    async def main() -> None:
        call = asyncio.ensure_future(ShieldedBlockingRunner(thread).run(boom))
        await thread.wait_in_flight(1, call)
        call.cancel()
        await yield_now()
        thread.release()
        with pytest.raises(asyncio.CancelledError):
            await call

    run(main())
