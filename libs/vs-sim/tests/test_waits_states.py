"""Bounded waits report the stuck peer; state waits wake on change, never by polling."""

from __future__ import annotations

import asyncio
import queue
import socket
import subprocess
import sys
import threading

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api.testing import (
    HANG_GUARD_S,
    Changes,
    VirtualClock,
    VirtualDeadlockError,
    accept_or_fail,
    get_or_fail,
    join_or_fail,
    run_virtual,
    stop_process,
    wait_for_async_state,
    wait_for_state,
    wait_or_fail,
)


def test_bounded_waits_return_what_a_finished_peer_delivered() -> None:
    event, items = threading.Event(), queue.Queue[int]()
    worker = threading.Thread(target=lambda: (items.put(5), event.set()))
    worker.start()
    wait_or_fail(event)
    assert get_or_fail(items) == 5
    join_or_fail(worker)


def test_accept_returns_the_connection() -> None:
    with socket.socket() as listener:
        listener.settimeout(HANG_GUARD_S)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with socket.create_connection(listener.getsockname(), timeout=HANG_GUARD_S) as client:
            client.settimeout(HANG_GUARD_S)
            connection, _ = accept_or_fail(listener)
            connection.close()
            assert client.fileno() >= 0


def test_stop_process_ends_a_process_that_ignores_nothing() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])
    stop_process(process)
    assert process.returncode is not None


def test_stop_process_escalates_when_the_process_ignores_sigterm() -> None:
    ready = (
        "import signal, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "print('ready', flush=True); signal.pause()"
    )
    process = subprocess.Popen(  # noqa: S603  # LW-163805 [S603]; argv is this interpreter and a fixed program, no untrusted input.
        [sys.executable, "-c", ready], stdout=subprocess.PIPE
    )
    assert process.stdout is not None
    assert process.stdout.readline() == b"ready\n"
    stop_process(process, grace_s=0)
    process.stdout.close()
    assert process.returncode == -9


class Counter:
    """A state with an owner that reports every change."""

    def __init__(self) -> None:
        self.value = 0
        self.changes = Changes()

    def set(self, value: int) -> None:
        self.value = value
        self.changes.notify()


@given(st.lists(st.integers(min_value=0, max_value=20), min_size=1, max_size=12))
def test_wait_for_state_returns_the_first_satisfying_state(updates: list[int]) -> None:
    counter = Counter()
    target = max(updates)

    async def producer() -> None:
        for value in updates:
            counter.set(value)
            await VirtualClock().sleep(0)

    async def main() -> int:
        task = asyncio.ensure_future(producer())
        reached = await wait_for_state(
            lambda: counter.value, lambda v: v >= target, counter.changes
        )
        await task
        return reached

    assert run_virtual(VirtualClock(), main()) >= target


def test_a_notification_between_observing_and_waiting_is_not_lost() -> None:
    counter = Counter()
    seen: list[int] = []

    def observe() -> int:
        value = counter.value
        if value == 0:
            counter.set(1)  # the state changes right after the observation
        seen.append(value)
        return value

    async def main() -> int:
        return await wait_for_state(observe, lambda v: v == 1, counter.changes)

    assert run_virtual(VirtualClock(), main()) == 1
    assert seen == [0, 1]


def test_a_state_that_never_holds_is_a_deadlock() -> None:
    counter = Counter()

    async def main() -> int:
        return await wait_for_state(lambda: counter.value, lambda v: v > 0, counter.changes)

    with pytest.raises(VirtualDeadlockError):
        run_virtual(VirtualClock(), main())


def test_wait_for_async_state_observes_through_a_coroutine() -> None:
    counter = Counter()

    async def observe() -> int:
        return counter.value

    async def main() -> int:
        counter.set(3)
        return await wait_for_async_state(observe, lambda v: v == 3, counter.changes)

    assert run_virtual(VirtualClock(), main()) == 3
