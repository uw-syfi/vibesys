"""``Versions``: what a read may show, for every way the application could have applied the writes.

The property: writes to one key are serialized (each starts after the last
ended, as an ``exclusive`` key makes them). An acknowledged write takes
effect at some instant within its call; a failed one at some instant after it
started, or never. A read over ``[start, end]`` sees the state of some
instant in ``[start - bound, end]``. Whatever the instants, what it sees is
allowed; and once every write is acknowledged and settled, only the last
value is.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys_evaluator.microservices import Call, Versions

DEFAULT = -1


def _call(start: float | None, end: float | None, *, ok: bool | None) -> Call:
    return Call(info={}, start=start, end=end, ok=ok, responses=())


@dataclass(frozen=True)
class Write:
    start: float
    end: float
    ok: bool
    applied_at: float | None  # None: a failed write that never took effect


@st.composite
def histories(draw: st.DrawFn) -> tuple[list[Write], float, float, float, float]:
    bound = draw(st.sampled_from([0.0, 0.5, 1.0]))
    now = 0.0
    writes: list[Write] = []
    for _ in range(draw(st.integers(0, 6))):
        start = now + draw(st.floats(0.0, 2.0))
        end = start + draw(st.floats(0.001, 1.0))
        ok = draw(st.booleans()) if draw(st.integers(0, 4)) == 0 else True
        if ok:
            applied: float | None = start + (end - start) * draw(st.floats(0.0, 1.0))
        else:
            applied = draw(st.one_of(st.none(), st.floats(start, start + 5.0)))
        writes.append(Write(start, end, ok=ok, applied_at=applied))
        now = end
    read_start = draw(st.floats(0.0, now + 3.0))
    read_end = read_start + draw(st.floats(0.0, 1.0))
    seen_at = draw(st.floats(max(read_start - bound, 0.0), read_end)) if read_end > 0 else 0.0
    return writes, bound, read_start, read_end, seen_at


def _state(writes: list[Write], at: float) -> int:
    applied = [
        (w.applied_at, n)
        for n, w in enumerate(writes)
        if w.applied_at is not None and w.applied_at <= at
    ]
    return max(applied)[1] if applied else DEFAULT


def _versions(writes: list[Write], bound: float) -> Versions[str, int]:
    versions: Versions[str, int] = Versions(bound_secs=bound)
    for n, w in enumerate(writes):
        versions.write("k", n, _call(w.start, w.end, ok=w.ok))
    return versions


@given(histories())
def test_what_a_read_can_see_is_always_allowed(
    history: tuple[list[Write], float, float, float, float],
) -> None:
    writes, bound, start, end, seen_at = history
    seen = _state(writes, seen_at)
    assert seen in _versions(writes, bound).allowed("k", DEFAULT, start=start, end=end)


@given(histories(), st.floats(0.001, 10.0))  # past the bound, beyond float rounding
def test_a_settled_key_allows_only_its_last_value(
    history: tuple[list[Write], float, float, float, float], later: float
) -> None:
    writes, bound, _, _, _ = history
    writes = [Write(w.start, w.end, ok=True, applied_at=w.start) for w in writes]
    last_end = writes[-1].end if writes else 0.0
    start = last_end + bound + later
    allowed = _versions(writes, bound).allowed("k", DEFAULT, start=start, end=start + 1.0)
    assert allowed == ((len(writes) - 1) if writes else DEFAULT,)


@given(histories())
def test_allowed_values_are_distinct_and_never_empty(
    history: tuple[list[Write], float, float, float, float],
) -> None:
    writes, bound, start, end, _ = history
    allowed = _versions(writes, bound).allowed("k", DEFAULT, start=start, end=end)
    assert allowed
    assert len(set(allowed)) == len(allowed)


def test_a_write_is_visible_once_it_started_and_required_once_settled() -> None:
    versions: Versions[str, str] = Versions(bound_secs=1.0)
    versions.write("k", "new", _call(10.0, 11.0, ok=True))
    assert versions.allowed("k", "old", start=5.0, end=9.0) == ("old",)
    assert versions.allowed("k", "old", start=5.0, end=10.5) == ("old", "new")
    assert versions.allowed("k", "old", start=11.5, end=12.0) == ("old", "new")  # within the bound
    assert versions.allowed("k", "old", start=12.0, end=12.5) == ("new",)


def test_a_failed_write_stays_possible() -> None:
    versions: Versions[str, str] = Versions(bound_secs=0.0)
    versions.write("k", "lost", _call(1.0, 2.0, ok=False))
    assert versions.allowed("k", "old", start=100.0, end=101.0) == ("old", "lost")


def test_latest_includes_pending_writes_and_other_keys_are_untouched() -> None:
    versions: Versions[str, str] = Versions()
    versions.write("k", "a", _call(1.0, 2.0, ok=True))
    versions.write("k", "b", _call(None, None, ok=None))
    assert versions.latest("k", "seed") == "b"
    assert versions.latest("other", "seed") == "seed"
    assert versions.allowed("k", "seed", start=10.0, end=11.0) == (
        "a",
    )  # a pending write was never sent


def test_a_negative_bound_is_refused() -> None:
    with pytest.raises(ValueError, match="bound_secs"):
        Versions(bound_secs=-0.1)
