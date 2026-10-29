"""The decision that ends a read-only server nobody holds, over generated readings."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from server.idle_watch import IdleWatch

_BOUND = st.integers(min_value=1, max_value=600).map(float)
_STEPS = st.lists(
    st.tuples(st.integers(min_value=0, max_value=3), st.integers(min_value=0, max_value=50)),
    max_size=40,
)
"""Readings in order: how many clients, and the whole seconds since the previous reading."""


def _replay(bound: float, steps: list[tuple[int, int]]) -> tuple[IdleWatch, float]:
    """The watch after each reading in order, and the time of the last one."""
    now = 0.0
    watch = IdleWatch.begin(bound, now)
    for clients, elapsed in steps:
        now += elapsed
        watch = watch.observe(clients, now)
    return watch, now


def _idle_start(steps: list[tuple[int, int]]) -> float | None:
    """When the current idle period began: the start, or the first empty reading after a client."""
    now = 0.0
    start: float | None = 0.0
    for clients, elapsed in steps:
        now += elapsed
        if clients > 0:
            start = None
        elif start is None:
            start = now
    return start


@given(bound=_BOUND, steps=_STEPS, wait=st.integers(min_value=0, max_value=1200))
def test_a_server_expires_exactly_when_it_has_been_idle_for_the_bound(
    bound: float, steps: list[tuple[int, int]], wait: int
) -> None:
    """Idle time counts from the first reading without a client, never while one holds it."""
    watch, now = _replay(bound, steps)
    idle_start = _idle_start(steps)
    assert watch.idle_since == idle_start
    later = now + wait
    expected = idle_start is not None and later - idle_start >= bound
    assert watch.expired(later) == expected


@given(bound=_BOUND, steps=_STEPS)
def test_a_server_a_client_holds_never_expires(bound: float, steps: list[tuple[int, int]]) -> None:
    watch, now = _replay(bound, [*steps, (1, 0)])
    assert not watch.expired(now + bound * 1000)


@given(bound=_BOUND)
def test_a_server_nobody_connects_to_expires_after_the_bound(bound: float) -> None:
    watch = IdleWatch.begin(bound, 10.0)
    assert not watch.expired(10.0 + bound - 1)
    assert watch.expired(10.0 + bound)
