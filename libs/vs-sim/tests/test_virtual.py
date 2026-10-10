"""The virtual clock shares one timeline among all waiters and never idles forever."""

from __future__ import annotations

import asyncio
import threading

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import (
    EventTrace,
    VirtualClock,
    VirtualDeadlockError,
    VirtualTimeLimitError,
    current_virtual_clock,
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


@example(durations=[0.5, 0.5 + 2.0**-52, 0.5])
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
    # The loop treats due times closer than its clock resolution as one instant, so adjacent
    # floats may share a jump; it never jumps more often than there are distinct due times.
    assert 1 <= len(advances) <= len({1.0 + seconds for seconds in durations})


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


def test_the_running_virtual_loop_knows_its_clock() -> None:
    clock = VirtualClock()

    async def main() -> VirtualClock:
        return current_virtual_clock()

    assert run_virtual(clock, main()) is clock


def test_a_real_loop_has_no_virtual_clock() -> None:
    async def main() -> None:
        current_virtual_clock()

    with pytest.raises(RuntimeError, match="not running on a virtual loop"):
        asyncio.run(main())


def _wake_order(count: int, seconds: float, schedule_seed: int | None) -> list[int]:
    """The order `count` tasks that sleep the same time, then record themselves, finish in."""
    clock = VirtualClock()
    woke: list[int] = []

    async def sleeper(index: int) -> None:
        await clock.sleep(seconds)
        woke.append(index)

    async def main() -> None:
        await asyncio.gather(*(sleeper(i) for i in range(count)))

    run_virtual(clock, main(), schedule_seed=schedule_seed)
    return woke


@given(st.integers(0, 2**32), st.integers(1, 20), st.floats(min_value=0.0, max_value=100.0))
def test_a_schedule_seed_replays_and_loses_no_work(seed: int, count: int, seconds: float) -> None:
    first = _wake_order(count, seconds, seed)
    assert first == _wake_order(count, seconds, seed)
    assert sorted(first) == list(range(count))


@given(st.integers(0, 2**32), st.integers(1, 20), st.floats(min_value=0.0, max_value=100.0))
def test_a_schedule_seed_keeps_distinct_due_times_in_order(
    seed: int, count: int, seconds: float
) -> None:
    """Perturbing ties never reorders sleeps that are really different lengths."""
    clock = VirtualClock()
    woke: list[int] = []

    async def sleeper(index: int) -> None:
        await clock.sleep(seconds + index)
        woke.append(index)

    async def main() -> None:
        await asyncio.gather(*(sleeper(i) for i in range(count)))

    run_virtual(clock, main(), schedule_seed=seed)
    assert woke == list(range(count))


def test_different_schedule_seeds_reach_different_orders() -> None:
    orders = {tuple(_wake_order(6, 1.0, seed)) for seed in range(40)}
    assert len(orders) > 10
    assert _wake_order(6, 1.0, None) == list(range(6))


# Seed 358 with four callbacks once left one of them unrun (#1674). The `ci` Hypothesis profile
# is derandomized with few examples, so a rare failing pair stays hidden: pin it, and search deeper.
@example(seed=358, count=4)
@settings(max_examples=300)
@given(st.integers(0, 2**32), st.integers(1, 12))
def test_ready_callbacks_run_in_a_seeded_order_and_all_run(seed: int, count: int) -> None:
    def order(schedule_seed: int | None) -> list[int]:
        ran: list[int] = []

        async def main() -> None:
            loop = asyncio.get_running_loop()
            for i in range(count):
                loop.call_soon(ran.append, i)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        run_virtual(VirtualClock(), main(), schedule_seed=schedule_seed)
        return ran

    assert order(None) == list(range(count))
    seeded = order(seed)
    assert seeded == order(seed)
    assert sorted(seeded) == list(range(count))


_PLANS = st.lists(
    st.tuples(
        st.sampled_from(["soon", "nested", "cancelled", "timer", "threadsafe"]),
        st.floats(min_value=0.0, max_value=3.0),
    ),
    min_size=1,
    max_size=12,
)


def _run_plan(plan: list[tuple[str, float]], schedule_seed: int | None) -> list[int]:
    """Run one callback per plan entry by its kind; return the ids that ran, in order."""
    clock = VirtualClock()
    ran: list[int] = []

    async def main() -> None:
        loop = asyncio.get_running_loop()
        for i, (kind, delay) in enumerate(plan):
            if kind == "soon":
                loop.call_soon(ran.append, i)
            elif kind == "nested":
                loop.call_soon(lambda i=i: loop.call_soon(ran.append, i))
            elif kind == "cancelled":
                loop.call_soon(ran.append, -1 - i).cancel()
                loop.call_soon(ran.append, i)
            elif kind == "timer":
                loop.call_later(delay, ran.append, i)
            else:
                await asyncio.to_thread(loop.call_soon_threadsafe, ran.append, i)
        await clock.sleep(10.0)

    run_virtual(clock, main(), schedule_seed=schedule_seed)
    return ran


@settings(max_examples=200)
@given(st.integers(0, 2**32), _PLANS)
def test_every_callback_runs_exactly_once_under_every_schedule(
    seed: int, plan: list[tuple[str, float]]
) -> None:
    """Callbacks scheduled from callbacks, threads, timers and cancelled handles are never lost."""
    expected = list(range(len(plan)))
    assert sorted(_run_plan(plan, None)) == expected
    seeded = _run_plan(plan, seed)
    assert sorted(seeded) == expected


@settings(max_examples=200)
@given(st.integers(0, 2**32), _PLANS)
def test_a_schedule_seed_replays_callbacks_scheduled_on_the_loop(
    seed: int, plan: list[tuple[str, float]]
) -> None:
    """Order is a function of the seed for work scheduled on the loop (a thread's hand-off is real time)."""
    on_loop = [(kind, delay) for kind, delay in plan if kind != "threadsafe"]
    if not on_loop:
        return
    assert _run_plan(on_loop, seed) == _run_plan(on_loop, seed)


_ADJACENT = st.lists(st.integers(0, 3).map(lambda k: 0.5 + k * 2.0**-52), min_size=1, max_size=8)


@example(durations=[0.5, 0.5 + 2.0**-52, 0.5], seed=None)
@settings(max_examples=300)
@given(_ADJACENT, st.none() | st.integers(0, 2**32))
def test_sleeps_wake_by_due_time_even_when_due_times_are_one_float_step_apart(
    durations: list[float], seed: int | None
) -> None:
    """Without a seed, a tie never lets a timer overtake one that is really due earlier; with one, none is lost."""
    clock = VirtualClock(1.0)
    woke: list[int] = []

    async def sleeper(index: int, seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append(index)

    async def main() -> None:
        await asyncio.gather(*(sleeper(i, d) for i, d in enumerate(durations)))

    run_virtual(clock, main(), schedule_seed=seed)
    due = [1.0 + d for d in durations]
    assert sorted(woke) == list(range(len(durations)))
    if seed is None:
        assert woke == sorted(range(len(durations)), key=lambda i: (due[i], i))
    # Under a seed, due times closer than the loop's clock resolution are one instant, and
    # the callbacks that instant makes ready run in a seeded order, so only the set is fixed.
