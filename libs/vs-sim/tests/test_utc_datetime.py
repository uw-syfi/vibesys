"""``utc_datetime`` names the calendar instant an epoch clock reads, in UTC."""

from __future__ import annotations

from datetime import UTC

from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import utc_datetime
from vs_sim.api.testing import ManualClock


@given(st.floats(min_value=0, max_value=4_000_000_000, allow_nan=False))
def test_the_datetime_is_the_clock_reading_in_utc(seconds: float) -> None:
    at = utc_datetime(ManualClock(seconds))

    assert at.tzinfo is UTC
    assert abs(at.timestamp() - seconds) < 1e-5


@given(st.floats(min_value=0, max_value=1_000_000), st.floats(min_value=0, max_value=1_000_000))
def test_a_later_reading_is_never_an_earlier_datetime(start: float, step: float) -> None:
    clock = ManualClock(start)
    before = utc_datetime(clock)
    clock.advance(step)

    assert utc_datetime(clock) >= before
