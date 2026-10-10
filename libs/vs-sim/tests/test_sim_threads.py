"""The thread simulator is deterministic per seed, explores other seeds, and shares the loop's clock."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import (
    HANG_GUARD_S,
    EventTrace,
    SimBlockingRunner,
    SimDeadlockError,
    SimNetwork,
    SimThreads,
    VirtualClock,
    VirtualDeadlockError,
    run_virtual,
)

type Step = tuple[str, int, float]
"""``(kind, object index, duration)`` for ``sleep``, ``locked``, ``set`` or ``wait``."""

STEPS = st.tuples(
    st.sampled_from(["sleep", "locked", "set", "wait"]),
    st.integers(0, 1),
    st.sampled_from([0.0, 0.5, 1.0, 2.5]),
)
PROGRAMS = st.lists(st.lists(STEPS, min_size=1, max_size=5), min_size=1, max_size=4)
SEEDS = st.integers(0, 2**32)


def _play(
    program: list[list[Step]], seed: int | None
) -> tuple[list[tuple[str, str, float]], int, EventTrace]:
    """Run each thread's steps; the log of what happened, the most threads inside a lock, the trace."""
    trace = EventTrace()
    sim = SimThreads(schedule_seed=seed, trace=trace)
    log: list[tuple[str, str, float]] = []
    inside = [0, 0]
    most = [0]

    def main() -> None:
        locks = [sim.lock(), sim.lock()]
        events = [sim.event(), sim.event()]

        def thread(index: int, steps: list[Step]) -> None:
            for kind, which, seconds in steps:
                if kind == "sleep":
                    sim.sleep(seconds)
                elif kind == "locked":
                    with locks[which]:
                        inside[which] += 1
                        most[0] = max(most[0], inside[which])
                        sim.sleep(seconds)
                        inside[which] -= 1
                elif kind == "set":
                    events[which].set()
                else:
                    events[which].wait(seconds)
                log.append((f"t{index}", kind, sim.now()))

        workers = [
            sim.spawn(lambda i=i, s=steps: thread(i, s), name=f"t{i}")
            for i, steps in enumerate(program)
        ]
        for worker in workers:
            worker.join(HANG_GUARD_S)

    sim.run(main)
    return log, most[0], trace


@settings(deadline=None, max_examples=60)
@given(PROGRAMS, SEEDS)
def test_one_seed_gives_one_interleaving(program: list[list[Step]], seed: int) -> None:
    first_log, _, first_trace = _play(program, seed)
    second_log, _, second_trace = _play(program, seed)
    assert first_log == second_log
    assert first_trace.first_difference(second_trace) is None


@settings(deadline=None, max_examples=60)
@given(PROGRAMS, st.one_of(st.none(), SEEDS))
def test_every_program_finishes_with_ordered_time_and_exclusive_locks(
    program: list[list[Step]], seed: int | None
) -> None:
    log, most_inside, _ = _play(program, seed)
    assert len(log) == sum(len(steps) for steps in program)
    instants = [at for _, _, at in log]
    assert instants == sorted(instants)
    assert most_inside <= 1


def test_different_seeds_explore_different_interleavings() -> None:
    def order(seed: int) -> tuple[str, ...]:
        sim = SimThreads(schedule_seed=seed)
        seen: list[str] = []

        def main() -> None:
            lock = sim.lock()

            def work(name: str) -> None:
                for _ in range(2):
                    with lock:
                        seen.append(name)

            for worker in [sim.spawn(lambda n=n: work(n), name=n) for n in "abc"]:
                worker.join(HANG_GUARD_S)

        sim.run(main)
        return tuple(seen)

    orders = {order(seed) for seed in range(40)}
    assert len(orders) > 3
    assert all(sorted(seen) == list("aabbcc") for seen in orders)


def test_without_a_seed_threads_run_in_spawn_order_until_they_block() -> None:
    sim = SimThreads()
    seen: list[str] = []

    def main() -> None:
        workers = [sim.spawn(lambda n=n: seen.extend([n, n]), name=n) for n in "abc"]
        for worker in workers:
            worker.join(HANG_GUARD_S)

    sim.run(main)
    assert seen == list("aabbcc")


@given(
    st.lists(st.floats(min_value=0.01, max_value=100.0), min_size=1, max_size=8),
    st.one_of(st.none(), SEEDS),
)
def test_sleepers_wake_in_due_order_and_the_run_lasts_as_long_as_the_longest(
    durations: list[float], seed: int | None
) -> None:
    clock = VirtualClock(1.0)
    sim = SimThreads(clock, schedule_seed=seed)
    woke: list[tuple[float, float]] = []

    def main() -> None:
        def sleeper(seconds: float) -> None:
            sim.sleep(seconds)
            woke.append((seconds, sim.now()))

        for worker in [sim.spawn(lambda s=s: sleeper(s), name="s") for s in durations]:
            worker.join(10 * HANG_GUARD_S)

    sim.run(main)
    assert [seconds for seconds, _ in woke] == sorted(durations)
    assert clock.now() == pytest.approx(1.0 + max(durations))


def test_threads_that_wait_on_each_other_in_a_cycle_are_a_deadlock_naming_them() -> None:
    sim = SimThreads()

    def main() -> None:
        first, second = sim.lock(), sim.lock()
        gate = sim.event()

        def forward() -> None:
            with first:
                gate.wait(1.0)
                with second:
                    pass

        def backward() -> None:
            with second:
                gate.wait(1.0)
                with first:
                    pass

        # Virtual time is free, so an unbounded join turns a deadlock into an error, not a hang.
        for worker in [sim.spawn(forward, name="forward"), sim.spawn(backward, name="backward")]:
            worker.join(None)

    with pytest.raises(SimDeadlockError) as error:
        sim.run(main)
    assert "forward" in str(error.value)
    assert "backward" in str(error.value)


def test_an_event_nobody_sets_is_a_deadlock_not_a_hang() -> None:
    sim = SimThreads()
    with pytest.raises(SimDeadlockError):
        sim.run(lambda: sim.event().wait())


def test_a_thread_that_raises_fails_the_run() -> None:
    sim = SimThreads()

    def boom() -> None:
        message = "worker failed"
        raise ValueError(message)

    def main() -> None:
        sim.spawn(boom, name="boom").join(HANG_GUARD_S)

    with pytest.raises(ValueError, match="worker failed"):
        sim.run(main)


def test_non_daemon_threads_finish_after_main_returns() -> None:
    sim = SimThreads()
    done: list[str] = []

    def main() -> None:
        sim.spawn(lambda: (sim.sleep(5.0), done.append("late")), name="late", daemon=False)

    sim.run(main)
    assert done == ["late"]
    assert sim.now() >= 5.0


def test_daemon_threads_are_unwound_when_main_returns() -> None:
    sim = SimThreads()
    unwound: list[str] = []

    def forever() -> None:
        try:
            sim.event().wait()
        finally:
            unwound.append("cleanup")

    sim.run(lambda: sim.spawn(forever, name="forever"))
    assert unwound == ["cleanup"]


@settings(deadline=None, max_examples=30)
@given(SEEDS, st.integers(1, 4))
def test_blocking_calls_from_a_coroutine_share_the_loops_timeline(seed: int, calls: int) -> None:
    clock = VirtualClock(0.0)
    trace = EventTrace()
    sim = SimThreads(clock, schedule_seed=seed, trace=trace)
    runner = SimBlockingRunner(sim)
    woke: list[str] = []

    def blocking(index: int) -> int:
        sim.sleep(10.0 * (index + 1))
        woke.append(f"thread{index}")
        return index

    async def main() -> list[int]:
        async def ticker() -> None:
            await clock.sleep(15.0)
            woke.append("loop")

        results = await asyncio.gather(
            ticker(), *(runner.run(blocking, index) for index in range(calls))
        )
        return [result for result in results[1:] if result is not None]

    assert run_virtual(clock, main(), schedule_seed=seed, driver=sim) == list(range(calls))
    expected = sorted([*(f"thread{i}" for i in range(calls)), "loop"], key=_due)
    assert woke == expected
    assert clock.now() == pytest.approx(max(15.0, 10.0 * calls))


def _due(name: str) -> float:
    return 15.0 if name == "loop" else 10.0 * (int(name.removeprefix("thread")) + 1)


def test_a_loop_waiting_on_a_thread_that_never_ends_reports_it() -> None:
    clock = VirtualClock()
    sim = SimThreads(clock)

    async def main() -> None:
        await SimBlockingRunner(sim).run(lambda: sim.event().wait())

    with pytest.raises(VirtualDeadlockError, match="blocking-0"):
        run_virtual(clock, main(), driver=sim)


@settings(deadline=None, max_examples=30)
@given(SEEDS, st.integers(1, 4))
def test_a_simulated_server_and_its_clients_replay_from_the_seed(seed: int, clients: int) -> None:
    def transcript() -> list[str]:
        sim = SimThreads(schedule_seed=seed)
        network = SimNetwork(sim)
        log: list[str] = []

        def main() -> None:
            listener = network.listen("srv")

            def serve() -> None:
                for _ in range(clients):
                    connection = listener.accept(HANG_GUARD_S)
                    request = connection.recv(16)
                    log.append(f"server:{request.decode()}")
                    connection.send(request.upper())
                    connection.close()

            def ask(index: int) -> None:
                connection = network.connect("srv")
                connection.send(f"c{index}".encode())
                log.append(f"client{index}:{connection.recv(16).decode()}")

            workers = [sim.spawn(serve, name="server")]
            workers += [sim.spawn(lambda i=i: ask(i), name=f"c{i}") for i in range(clients)]
            for worker in workers:
                worker.join(HANG_GUARD_S)

        sim.run(main)
        return log

    first = transcript()
    assert first == transcript()
    assert sorted(entry for entry in first if entry.startswith("client")) == [
        f"client{i}:C{i}" for i in range(clients)
    ]
