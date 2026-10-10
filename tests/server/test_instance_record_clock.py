"""A web instance record takes its start time from the clock it is given."""

from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from server.transport.discovery import WebInstanceRecord
from vs_sim.api.testing import ManualClock


@given(
    start=st.floats(min_value=0, max_value=4e9, allow_nan=False),
    pid=st.integers(1, 2**22),
    secret=st.text(),
)
def test_started_at_is_the_clock_reading(start: float, pid: int, secret: str) -> None:
    clock = ManualClock(start)

    record = WebInstanceRecord.from_gateway(
        pid=pid, port=1, token=secret, project_root=Path(), clock=clock
    )

    assert record.started_at == start
