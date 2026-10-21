"""A synchronous owner can drive worker acknowledgements without running its loop."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api.testing import (
    HANG_GUARD_S,
    SimDeadlockError,
    SimThreads,
    VirtualClock,
    loop_threads,
    run_virtual,
)


@given(seed=st.integers(0, 2**32 - 1))
def test_external_waits_release_and_reacquire_locks_and_join_workers(seed: int) -> None:
    threads = SimThreads(schedule_seed=seed)
    lock = threads.lock()
    entered = threads.event()
    release = threads.event()

    def worker() -> None:
        with lock:
            entered.set()
            assert release.wait(HANG_GUARD_S)

    child = threads.spawn(worker, name="worker")
    assert entered.wait(HANG_GUARD_S)
    release.set()
    with lock:
        pass
    child.join(HANG_GUARD_S)
    assert not child.is_alive()
    threads.close()


def test_external_condition_wait_releases_all_lock_depth_and_restores_it() -> None:
    threads = SimThreads()
    condition = threads.condition()
    seen: list[str] = []

    def worker() -> None:
        with condition:
            seen.append("worker")
            condition.notify_all()

    with condition, condition:
        child = threads.spawn(worker, name="notifier")
        assert condition.wait(HANG_GUARD_S)
        seen.append("owner")
    child.join(HANG_GUARD_S)
    assert seen == ["worker", "owner"]
    threads.close()


def test_external_unproduced_wait_is_a_deadlock_and_can_be_closed() -> None:
    threads = SimThreads()
    lock = threads.lock()
    with lock, pytest.raises(SimDeadlockError, match="no timer"):
        lock.acquire()
    threads.close()


def test_borrowed_workers_share_clock_and_outlive_owner_loop() -> None:
    clock = VirtualClock()
    threads = SimThreads()
    arrived = threads.event()
    stop = threads.event()
    seen: list[float] = []

    def worker() -> None:
        arrived.set()
        assert stop.wait(HANG_GUARD_S)
        seen.append(threads.clock.now())

    child = threads.spawn(worker, name="persistent")

    async def exercise() -> None:
        assert arrived.wait(HANG_GUARD_S)
        await clock.sleep(0.2)

    run_virtual(clock, exercise())
    assert child.is_alive()
    stop.set()
    child.join(HANG_GUARD_S)
    assert seen == [pytest.approx(1.2)]
    threads.close()


def test_multiple_external_simulators_share_one_loop_timeline() -> None:
    clock = VirtualClock()
    owners = [SimThreads(), SimThreads()]
    events = [owner.event() for owner in owners]

    async def exercise() -> None:
        for owner, event in zip(owners, events, strict=True):
            owner.spawn(lambda o=owner, e=event: (o.sleep(0.1), e.set()), name="timer")
            assert event.wait(HANG_GUARD_S)
        assert clock.now() == pytest.approx(1.2)

    run_virtual(clock, exercise())
    for owner in owners:
        owner.close()


@given(seed=st.integers(0, 2**32 - 1))
def test_external_contended_lock_receives_ownership_from_worker(seed: int) -> None:
    threads = SimThreads(schedule_seed=seed)
    lock = threads.lock()
    entered = threads.event()
    seen: list[str] = []

    def worker() -> None:
        with lock:
            entered.set()
            threads.sleep(0.1)
            seen.append("worker")

    child = threads.spawn(worker, name="holder")
    assert entered.wait(HANG_GUARD_S)
    with lock:
        seen.append("owner")
    child.join(HANG_GUARD_S)
    assert seen == ["worker", "owner"]
    threads.close()


def test_loop_threads_reuses_the_borrowed_scheduler() -> None:
    clock = VirtualClock()
    threads = SimThreads()
    ready = threads.event()

    async def exercise() -> None:
        child = threads.spawn(ready.set, name="ready")
        assert ready.wait(HANG_GUARD_S)
        assert loop_threads() is threads
        child.join(HANG_GUARD_S)

    run_virtual(clock, exercise())
    threads.close()


def test_external_workers_compose_with_an_owned_loop_driver() -> None:
    clock = VirtualClock()
    owned = SimThreads(clock)
    external = SimThreads()
    owned_stop = owned.event()
    external_ready = external.event()
    external_stop = external.event()
    workers = []

    def external_worker() -> None:
        external_ready.set()
        assert external_stop.wait(HANG_GUARD_S)

    async def exercise() -> None:
        workers.append(owned.spawn(lambda: owned_stop.wait(HANG_GUARD_S), name="owned"))
        workers.append(external.spawn(external_worker, name="external"))
        assert external_ready.wait(HANG_GUARD_S)
        assert loop_threads() is owned
        await clock.sleep(0.2)

    run_virtual(clock, exercise(), driver=owned)
    assert not workers[0].is_alive()
    assert workers[1].is_alive()
    external_stop.set()
    workers[1].join(HANG_GUARD_S)
    assert not workers[1].is_alive()
    external.close()
