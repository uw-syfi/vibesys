"""The statistic behind a comparison: what a verdict is allowed to depend on (#3047).

A comparison is of the candidate's samples with the base's. These properties hold for any
samples: the order they were taken in changes nothing, a unit change (scaling every value)
changes nothing, flipping which way is better flips better and worse, more noise never turns
a within-noise result into a verdict, and the noise never falls below what the task declares
for the samples it has.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

if TYPE_CHECKING:
    import random

type Direction = Literal["max", "min"] | None

SCOPE = core.Scope(owner=core.AttemptId(root="attempt"), generation=0)
ANCHOR = core.BaseRef(commit=core.RevisionRef.of_git_commit("base"), measured_input="base-input")
VALUES = st.floats(min_value=1.0, max_value=1e4, allow_nan=False, allow_infinity=False)
FLOORS = st.floats(min_value=0.0, max_value=0.2, allow_nan=False)


def sample(value: float, *, direction: Direction = "max", measured_input: str = "x") -> core.Sample:
    return core.Sample(
        run=core.RequestId(root=f"run-{measured_input}-{value!r}"),
        scope=SCOPE,
        commit=core.RevisionRef.of_git_commit("candidate"),
        measured_input=measured_input,
        evaluation="evaluation",
        gates=("accuracy", "benchmark"),
        host="host",
        dedicated_resources=True,
        reading=core.Reading(metric="rate", value=value, direction=direction),
        observed_at=0.0,
        base=ANCHOR if measured_input != "base-input" else None,
    )


def compared(
    mine: list[float],
    theirs: list[float],
    *,
    floor: float = 0.0,
    direction: Direction = "max",
) -> core.Comparison:
    result = core.compare(
        tuple(sample(v, direction=direction) for v in mine),
        tuple(sample(v, direction=direction, measured_input="base-input") for v in theirs),
        scope=SCOPE,
        anchor=ANCHOR,
        policy=core.ComparisonPolicy(noise_floor=floor),
    )
    assert result is not None
    return result


@given(
    mine=st.lists(VALUES, min_size=1, max_size=6), theirs=st.lists(VALUES, min_size=1, max_size=6)
)
def test_the_ratio_is_the_mean_of_the_candidate_over_the_mean_of_the_base(
    mine: list[float], theirs: list[float]
) -> None:
    result = compared(mine, theirs)
    assert math.isclose(result.candidate_value, sum(mine) / len(mine))
    assert math.isclose(result.base_value, sum(theirs) / len(theirs))
    assert math.isclose(result.ratio, result.candidate_value / result.base_value)
    assert (result.candidate_samples, result.base_samples) == (len(mine), len(theirs))


@given(
    mine=st.lists(VALUES, min_size=1, max_size=6),
    theirs=st.lists(VALUES, min_size=1, max_size=6),
    floor=FLOORS,
    seed=st.randoms(use_true_random=False),
)
def test_the_order_of_the_samples_changes_nothing(
    mine: list[float], theirs: list[float], floor: float, seed: random.Random
) -> None:
    shuffled_mine, shuffled_theirs = list(mine), list(theirs)
    seed.shuffle(shuffled_mine)
    seed.shuffle(shuffled_theirs)
    a = compared(mine, theirs, floor=floor)
    b = compared(shuffled_mine, shuffled_theirs, floor=floor)
    assert math.isclose(a.ratio, b.ratio)
    assert math.isclose(a.noise, b.noise, abs_tol=1e-12)
    assert a.verdict is b.verdict


@given(
    mine=st.lists(VALUES, min_size=1, max_size=6),
    theirs=st.lists(VALUES, min_size=1, max_size=6),
    floor=FLOORS,
    scale=st.floats(min_value=0.01, max_value=100.0),
)
def test_a_unit_change_changes_no_ratio_noise_or_verdict(
    mine: list[float], theirs: list[float], floor: float, scale: float
) -> None:
    a = compared(mine, theirs, floor=floor)
    b = compared([v * scale for v in mine], [v * scale for v in theirs], floor=floor)
    assert math.isclose(a.ratio, b.ratio, rel_tol=1e-9)
    assert math.isclose(a.noise, b.noise, rel_tol=1e-6, abs_tol=1e-12)
    assert a.verdict is b.verdict


@given(
    mine=st.lists(VALUES, min_size=1, max_size=6),
    theirs=st.lists(VALUES, min_size=1, max_size=6),
    floor=FLOORS,
)
def test_which_way_is_better_flips_better_and_worse_and_nothing_else(
    mine: list[float], theirs: list[float], floor: float
) -> None:
    high = compared(mine, theirs, floor=floor, direction="max")
    low = compared(mine, theirs, floor=floor, direction="min")
    flipped = {
        core.ComparisonVerdict.BETTER: core.ComparisonVerdict.WORSE,
        core.ComparisonVerdict.WORSE: core.ComparisonVerdict.BETTER,
        core.ComparisonVerdict.WITHIN_NOISE: core.ComparisonVerdict.WITHIN_NOISE,
    }
    assert (high.ratio, high.noise) == (low.ratio, low.noise)
    assert low.verdict is flipped[high.verdict]


@given(
    mine=st.lists(VALUES, min_size=1, max_size=6),
    theirs=st.lists(VALUES, min_size=1, max_size=6),
    floor=FLOORS,
)
def test_the_noise_is_never_below_what_the_declared_floor_gives_the_samples_held(
    mine: list[float], theirs: list[float], floor: float
) -> None:
    result = compared(mine, theirs, floor=floor)
    least = abs(result.ratio) * floor * math.sqrt(1 / len(mine) + 1 / len(theirs))
    assert result.noise >= least - 1e-12


@given(
    mine=st.lists(VALUES, min_size=1, max_size=6),
    theirs=st.lists(VALUES, min_size=1, max_size=6),
    floor=FLOORS,
    extra=st.floats(min_value=0.0, max_value=0.2),
)
def test_a_higher_floor_never_turns_an_undecided_result_into_a_verdict(
    mine: list[float], theirs: list[float], floor: float, extra: float
) -> None:
    low = compared(mine, theirs, floor=floor)
    high = compared(mine, theirs, floor=floor + extra)
    if low.verdict is core.ComparisonVerdict.WITHIN_NOISE:
        assert high.verdict is core.ComparisonVerdict.WITHIN_NOISE
    assert high.noise >= low.noise - 1e-12


@given(
    mine=st.lists(VALUES, min_size=1, max_size=4), theirs=st.lists(VALUES, min_size=1, max_size=4)
)
def test_a_measurement_without_a_known_direction_or_a_base_is_not_compared(
    mine: list[float], theirs: list[float]
) -> None:
    undirected = core.compare(
        tuple(sample(v, direction=None) for v in mine),
        tuple(sample(v, direction=None, measured_input="base-input") for v in theirs),
        scope=SCOPE,
        anchor=ANCHOR,
        policy=core.ComparisonPolicy(),
    )
    assert undirected is None
    assert (
        core.compare((), (sample(1.0),), scope=SCOPE, anchor=ANCHOR, policy=core.ComparisonPolicy())
        is None
    )
