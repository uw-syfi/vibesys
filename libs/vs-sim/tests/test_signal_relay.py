"""``SignalRelay`` and ``MainThreadWake``: the Fakes and the real ones pass the same contracts.

The real ones change process-wide signal state, so each of their cases runs in a forked child.
"""

from __future__ import annotations

import os
import signal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import Event, OsThreads, SignalMainThreadWake, WakeupFdSignalRelay
from vs_sim.api.testing import (
    FakeMainThreadWake,
    FakeSignalRelay,
    MainThreadWakeContract,
    MainThreadWakeUnderTest,
    RelayUnderTest,
    SignalRelayContract,
    run_in_child,
    wait_or_fail,
)


def _already_arrived(event: Event, what: str) -> None:
    # The Fake calls the callback inside ``deliver``, so there is nothing to wait for.
    assert event.is_set(), what


class TestFakeSignalRelay(SignalRelayContract):
    def subject(self) -> RelayUnderTest:
        relay = FakeSignalRelay()
        return RelayUnderTest(
            relay=relay,
            numbers=(_FIRST, _SECOND),
            deliver=relay.deliver,
            threads=OsThreads(),
            run=lambda body: body(),
            wait=_already_arrived,
        )


_FIRST, _SECOND, _TERM = 10, 12, 15
_NUMBERS = st.sampled_from([_FIRST, _SECOND, _TERM, 2])


@given(
    watched=st.sets(_NUMBERS),
    arrivals=st.lists(_NUMBERS, max_size=12),
)
def test_only_watched_signals_are_delivered_and_only_while_active(
    watched: set[int], arrivals: list[int]
) -> None:
    relay = FakeSignalRelay()
    received: list[int] = []
    assert not relay.deliver(_TERM)
    with relay.relay(watched, received.append):
        assert relay.active
        for number in arrivals:
            assert relay.deliver(number) == (number in watched)
    assert not relay.active
    assert not relay.deliver(_TERM)
    assert received == [n for n in arrivals if n in watched]


def test_a_second_block_cannot_open_while_one_is_active() -> None:
    relay = FakeSignalRelay()
    with relay.relay([_TERM], lambda _n: None), pytest.raises(RuntimeError):
        relay.relay([2], lambda _n: None).__enter__()


class TestWakeupFdSignalRelay(SignalRelayContract):
    def subject(self) -> RelayUnderTest:
        return RelayUnderTest(
            relay=WakeupFdSignalRelay(OsThreads()),
            numbers=(signal.SIGUSR1, signal.SIGUSR2),
            deliver=lambda number: os.kill(os.getpid(), number),
            threads=OsThreads(),
            run=run_in_child,
            wait=wait_or_fail,
        )


class TestFakeMainThreadWake(MainThreadWakeContract):
    def subject(self) -> MainThreadWakeUnderTest:
        wake = FakeMainThreadWake()
        return MainThreadWakeUnderTest(
            wake=wake,
            answer=wake.answer,
            threads=OsThreads(),
            run=lambda body: body(),
            wait=_already_arrived,
        )


class TestSignalMainThreadWake(MainThreadWakeContract):
    def subject(self) -> MainThreadWakeUnderTest:
        return MainThreadWakeUnderTest(
            wake=SignalMainThreadWake(signal.SIGUSR2),
            answer=lambda: None,
            threads=OsThreads(),
            run=run_in_child,
            wait=wait_or_fail,
        )


def _wake_restores_the_earlier_handler() -> bool:
    def earlier(_number: int, _frame: object) -> None:
        return None

    signal.signal(signal.SIGUSR2, earlier)
    with SignalMainThreadWake(signal.SIGUSR2).armed(lambda: None):
        pass
    return signal.getsignal(signal.SIGUSR2) is earlier


def test_the_real_wake_restores_the_handler_it_replaced() -> None:
    assert run_in_child(_wake_restores_the_earlier_handler)


def test_the_fake_wake_counts_sends_and_answers_only_while_armed() -> None:
    wake = FakeMainThreadWake()
    taken: list[None] = []
    assert not wake.answer()
    with wake.armed(lambda: taken.append(None)) as send:
        assert wake.active
        send()
        send()
        assert wake.answer()
    assert (wake.sent, len(taken), wake.active, wake.answer()) == (2, 1, False, False)
