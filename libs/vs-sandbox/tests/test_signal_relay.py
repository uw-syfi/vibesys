"""``SignalRelay`` through the Fake; the real ``WakeupFdSignalRelay`` passes the same contract in ``tests/e2e``."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api.testing import FakeSignalRelay, RelayUnderTest, SignalRelayContract
from vs_sim.api import Event, OsThreads


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
