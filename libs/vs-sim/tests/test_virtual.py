"""The virtual clock shares one timeline among all waiters and never idles forever."""

from __future__ import annotations

import asyncio
import threading

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_sim.api.testing import (
    EventTrace,
    VirtualClock,
    VirtualDeadlockError,
    VirtualTimeLimitError,
    run_virtual,
    wait_or_fail,
)

DURATIONS = st.lists(st.floats(min_value=0.01, max_value=500.0), min_size=1, max_size=8)


@example(durations=[0.010000000000000002, 0.01])
@given(DURATIONS)
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
    # Durations that differ by one ulp can land on the same due time (1.0 + 0.01 ==
    # 1.0 + 0.010000000000000002), and ties wake in start order, so order by due time.
    assert [seconds for seconds, _ in woke] == sorted(durations, key=lambda seconds: 1.0 + seconds)
    assert all(at == pytest.approx(1.0 + seconds, abs=1e-6) for seconds, at in woke)


@given(st.integers(min_value=1, max_value=40), st.floats(min_value=0.0, max_value=100.0))
def test_equal_sleeps_wake_in_start_order_however_many_there_are(
    count: int, seconds: float
) -> None:
    """Regression: asyncio's timer heap is not stable, so equal due times popped out of order."""
    clock = VirtualClock()
    woke: list[int] = []

    async def sleeper(index: int) -> None:
        await clock.sleep(seconds)
        woke.append(index)

    async def main() -> None:
        await asyncio.gather(*(sleeper(i) for i in range(count)))

    run_virtual(clock, main())
    assert woke == list(range(count))


def test_a_run_nothing_can_wake_fails_instead_of_hanging() -> None:
    async def main() -> None:
        await asyncio.Event().wait()

    with pytest.raises(VirtualDeadlockError, match="no timer is scheduled"):
        run_virtual(VirtualClock(), main())


def test_a_deadlock_names_the_tasks_that_wait() -> None:
    async def stuck_forever() -> None:
        await asyncio.Event().wait()

    with pytest.raises(VirtualDeadlockError, match="stuck_forever"):
        run_virtual(VirtualClock(), stuck_forever())


def test_time_stands_still_while_worker_threads_run() -> None:
    """A sleeper due far in the future does not wake before the workers finish."""
    clock = VirtualClock(at=0.0)
    go = threading.Event()
    order: list[str] = []

    def waiter() -> None:
        wait_or_fail(go, "the second worker")
        order.append(f"first worker at {clock.now():g}")

    def releaser() -> None:
        go.set()
        order.append(f"second worker at {clock.now():g}")

    async def sleeper() -> None:
        await clock.sleep(1000.0)
        order.append(f"sleeper at {clock.now():g}")

    async def main() -> None:
        later = asyncio.ensure_future(sleeper())
        await asyncio.gather(asyncio.to_thread(waiter), asyncio.to_thread(releaser))
        await later

    run_virtual(clock, main())
    assert sorted(order[:2]) == ["first worker at 0", "second worker at 0"]
    assert order[2] == "sleeper at 1000"


def test_a_cancelled_worker_wait_stops_holding_the_clock() -> None:
    clock = VirtualClock(at=0.0)
    release = threading.Event()

    def hold() -> None:
        wait_or_fail(release, "the release")

    async def main() -> float:
        worker = asyncio.ensure_future(asyncio.to_thread(hold))
        await asyncio.sleep(0)
        worker.cancel()
        await clock.sleep(5.0)
        release.set()
        return clock.now()

    assert run_virtual(clock, main()) == 5.0


@given(st.lists(st.floats(min_value=0.0, max_value=100.0), max_size=8))
def test_sleeps_are_recorded_and_the_limit_stops_a_run_that_would_pass_it(
    durations: list[float],
) -> None:
    limit = 150.0
    clock = VirtualClock(at=0.0, limit=limit)

    async def main() -> None:
        for seconds in durations:
            await clock.sleep(seconds)

    if sum(durations) > limit:
        with pytest.raises(VirtualTimeLimitError):
            run_virtual(clock, main())
    else:
        run_virtual(clock, main())
        assert clock.sleeps == durations


@given(DURATIONS)
def test_tasks_left_running_are_cancelled_when_main_returns(durations: list[float]) -> None:
    clock = VirtualClock()
    cancelled: list[float] = []

    async def background(seconds: float) -> None:
        try:
            await clock.sleep(seconds + 1000.0)
        except asyncio.CancelledError:
            cancelled.append(seconds)
            raise

    async def main() -> None:
        running = [asyncio.ensure_future(background(seconds)) for seconds in durations]
        await clock.sleep(0.001)
        assert not any(task.done() for task in running)

    run_virtual(clock, main())
    assert sorted(cancelled) == sorted(durations)


def test_a_failure_in_main_propagates_and_leaves_no_loop_behind() -> None:
    async def main() -> None:
        raise KeyError("boom")

    with pytest.raises(KeyError):
        run_virtual(VirtualClock(), main())
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()


@given(DURATIONS)
def test_the_same_run_leaves_the_same_trace(durations: list[float]) -> None:
    def trace_of_one_run() -> EventTrace:
        trace = EventTrace()
        clock = VirtualClock()

        async def sleeper(seconds: float) -> None:
            await clock.sleep(seconds)

        async def main() -> None:
            await asyncio.gather(*(sleeper(seconds) for seconds in durations))

        run_virtual(clock, main(), trace=trace)
        return trace

    first, second = trace_of_one_run(), trace_of_one_run()
    assert first.first_difference(second) is None
    advances = [event for event in first.events if event[0] == "advance"]
    assert len(advances) == len({1.0 + seconds for seconds in durations})


def test_the_trace_records_task_order_and_clock_jumps() -> None:
    trace = EventTrace()
    clock = VirtualClock(at=0.0)

    async def worker() -> None:
        await clock.sleep(5.0)

    async def main() -> None:
        await asyncio.gather(worker(), worker())

    run_virtual(clock, main(), trace=trace)
    kinds = [event[0] for event in trace.events]
    assert kinds.count("advance") == 1
    assert ("advance", "0.0", "5.0") in trace.events
    assert [event[1] for event in trace.events if event[0] == "step"][:3] == [
        "test_the_trace_records_task_order_and_clock_jumps.<locals>.main#0",
        "test_the_trace_records_task_order_and_clock_jumps.<locals>.worker#1",
        "test_the_trace_records_task_order_and_clock_jumps.<locals>.worker#2",
    ]


def test_a_run_that_reads_the_wall_clock_leaves_a_different_trace() -> None:
    """The determinism check's reason to exist: behaviour that follows something outside the clock."""
    outside = iter([3.0, 7.0])

    def trace_of_one_run() -> EventTrace:
        trace = EventTrace()
        clock = VirtualClock()

        async def main() -> None:
            await clock.sleep(next(outside))

        run_virtual(clock, main(), trace=trace)
        return trace

    first, second = trace_of_one_run(), trace_of_one_run()
    assert first.first_difference(second) is not None


def test_traces_of_different_length_differ() -> None:
    short, long = EventTrace(), EventTrace()
    long.step("a")
    assert short.first_difference(long) is not None
    assert long.first_difference(long) is None
