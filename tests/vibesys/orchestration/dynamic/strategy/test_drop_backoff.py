"""The wait before a lost turn is asked again grows with each drop and is bounded."""

from __future__ import annotations

from itertools import pairwise

from hypothesis import example, given
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.strategy._run import config

_BASE = st.floats(min_value=0.01, max_value=600.0)
_CAP = st.floats(min_value=0.01, max_value=3600.0)


@example(base=5.0, cap=120.0, drops=10)
@given(base=_BASE, cap=_CAP, drops=st.integers(min_value=0, max_value=40))
def test_the_backoff_never_shrinks_never_passes_the_cap_and_starts_at_the_base(
    base: float, cap: float, drops: int
) -> None:
    policy = config(turn_drop_backoff_seconds=base, turn_drop_backoff_cap_seconds=cap)

    waits = [policy.drop_backoff(n) for n in range(drops + 1)]

    assert waits[0] == min(base, cap)
    assert all(0 < wait <= cap for wait in waits)
    assert waits == sorted(waits)
    for earlier, later in pairwise(waits):
        assert later == min(cap, earlier * 2) or later == cap
