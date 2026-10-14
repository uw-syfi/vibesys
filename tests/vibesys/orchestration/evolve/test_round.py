"""A round admits its children in slot order, whatever order they finish in."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve._values import failing, passing

from vibesys.orchestration.evolve.search.api import Round
from vs_sim.api.testing import examples


def round_of(children: int, width: int) -> tuple[Round, list]:
    dones = [passing(f"g1.c{n}", f"c{n}", 100.0, cold=False) for n in range(1, children + 1)]
    return Round(starts=tuple(d.start for d in dones), width=width, generation_open=True), dones


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    children=st.integers(1, 6),
    width=st.integers(1, 4),
    order=st.randoms(use_true_random=False),
)
def test_admission_is_in_slot_order_exactly_once_whatever_order_children_finish_in(
    children: int, width: int, order: object
) -> None:
    round_, dones = round_of(children, width)
    running: list = []
    admitted: list[str] = []
    while len(admitted) < children:
        running.extend(
            next(d for d in dones if d.start == start)
            for start in round_.ready(width - len(running), stopping=False)
        )
        assert len(running) <= width
        done = running.pop(order.randrange(len(running)))  # ty: ignore[unresolved-attribute]
        admitted.extend(item.start.key for item in round_.finish(done))
    assert admitted == [d.start.key for d in dones]
    assert round_.ready(width, stopping=False) == []


@settings(max_examples=examples(pr=8, nightly=100))
@given(children=st.integers(1, 6), free=st.integers(-1, 8))
def test_ready_never_starts_more_than_the_free_slots_and_nothing_once_stopping(
    children: int, free: int
) -> None:
    round_, dones = round_of(children, 4)
    assert round_.ready(free, stopping=True) == []
    started = round_.ready(free, stopping=False)
    assert len(started) == min(max(free, 0), children)
    assert [s.key for s in started] == [d.start.key for d in dones[: len(started)]]


def test_a_child_waits_for_the_lower_slot_before_it_is_admitted() -> None:
    round_, dones = round_of(2, 2)
    round_.ready(2, stopping=False)
    assert round_.finish(dones[1]) == []
    assert [d.start.key for d in round_.finish(dones[0])] == ["g1.c1", "g1.c2"]


def test_a_failed_child_is_admitted_like_any_other() -> None:
    round_ = Round(starts=(failing("g1.c1", cold=False).start,), width=1, generation_open=True)
    round_.ready(1, stopping=False)
    assert len(round_.finish(failing("g1.c1", cold=False))) == 1
