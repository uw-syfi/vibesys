"""A gate ends with the operation that should have opened it."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api.testing import (
    Gate,
    VirtualClock,
    VirtualDeadlockError,
    arrival,
    join_or_fail,
    run_virtual,
    wait_until_started,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine


def run[T](main: Coroutine[object, object, T]) -> T:
    return run_virtual(VirtualClock(), main)


def test_a_gate_opened_before_the_wait_returns_at_once() -> None:
    gate = Gate()
    gate.open()

    async def main() -> None:
        await gate.wait()

    run(main())
    assert gate.is_open


@given(st.integers(min_value=1, max_value=10))
def test_opening_releases_every_waiter(waiters: int) -> None:
    gate = Gate()
    released: list[int] = []

    async def waiter(index: int) -> None:
        await gate.wait()
        released.append(index)

    async def main() -> None:
        tasks = [asyncio.ensure_future(waiter(i)) for i in range(waiters)]
        await asyncio.sleep(0)
        assert released == []
        gate.open()
        await asyncio.gather(*tasks)

    run(main())
    assert released == list(range(waiters))


def test_an_operation_that_fails_first_ends_the_wait_with_its_own_error() -> None:
    gate = Gate()

    async def operation() -> None:
        message = "the operation's own failure"
        raise KeyError(message)

    async def main() -> None:
        await gate.wait(asyncio.ensure_future(operation()))

    with pytest.raises(KeyError, match="own failure"):
        run(main())


def test_an_operation_that_returns_without_opening_the_gate_fails_the_wait() -> None:
    gate = Gate()

    async def operation() -> None:
        return None

    async def main() -> None:
        await gate.wait(asyncio.ensure_future(operation()))

    with pytest.raises(AssertionError, match="without the arrival"):
        run(main())


def test_an_arrival_wins_over_an_operation_that_ended_after_it() -> None:
    gate = Gate()

    async def operation() -> None:
        gate.open()

    async def main() -> None:
        await gate.wait(asyncio.ensure_future(operation()))

    run(main())


def test_a_gate_nothing_opens_is_a_deadlock_not_a_hang() -> None:
    gate = Gate()

    async def main() -> None:
        await gate.wait()

    with pytest.raises(VirtualDeadlockError):
        run(main())


def test_a_gate_can_be_opened_from_another_thread() -> None:
    """On a real loop, a worker thread opens the gate and the waiter on the loop is released."""
    gate = Gate()
    worker = threading.Thread(target=gate.open)

    async def main() -> None:
        worker.start()
        await gate.wait()

    asyncio.run(main())
    join_or_fail(worker)
    assert gate.is_open


def test_arrival_returns_the_awaited_value() -> None:
    async def main() -> int:
        queue: asyncio.Queue[int] = asyncio.Queue()
        queue.put_nowait(7)
        return await arrival(queue.get())

    assert run(main()) == 7


@pytest.mark.parametrize("flavour", ["asyncio", "threading"])
def test_wait_until_started_ends_with_the_operation(flavour: str) -> None:
    started = asyncio.Event() if flavour == "asyncio" else threading.Event()

    async def operation() -> None:
        message = "failed before starting"
        raise ValueError(message)

    async def main() -> None:
        await wait_until_started(started, asyncio.ensure_future(operation()))

    if flavour == "threading":
        with pytest.raises(ValueError, match="failed before"):
            asyncio.run(main())
    else:
        with pytest.raises(ValueError, match="failed before"):
            run(main())
