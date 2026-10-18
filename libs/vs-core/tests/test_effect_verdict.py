"""The framework's verdict on a plan's expected effect, derived from a comparison (#3047).

A plan states a typed bar, a minimum ratio over the base in the metric's own direction. What the
measured comparison says of it is derived, never declared: `supported` when the candidate clears
the bar by more than two noises, `disproven` when it misses by more than that, and
`within_noise` otherwise. These properties hold for any comparison and any bar.
"""

from __future__ import annotations

from typing import Literal

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_comparison_statistic import FLOORS, VALUES, compared

type Direction = Literal["max", "min"]

DIRECTIONS = st.sampled_from(["max", "min"])
BARS = st.floats(min_value=0.2, max_value=3.0, allow_nan=False)
SAMPLES = st.lists(VALUES, min_size=1, max_size=5)

_RANK = {
    core.EffectVerdict.SUPPORTED: 2,
    core.EffectVerdict.WITHIN_NOISE: 1,
    core.EffectVerdict.DISPROVEN: 0,
}
_AS_COMPARED = {
    core.ComparisonVerdict.BETTER: core.EffectVerdict.SUPPORTED,
    core.ComparisonVerdict.WITHIN_NOISE: core.EffectVerdict.WITHIN_NOISE,
    core.ComparisonVerdict.WORSE: core.EffectVerdict.DISPROVEN,
}


@given(mine=SAMPLES, theirs=SAMPLES, floor=FLOORS, direction=DIRECTIONS)
def test_a_bar_of_one_is_the_comparisons_own_verdict(
    mine: list[float], theirs: list[float], floor: float, direction: Direction
) -> None:
    result = compared(mine, theirs, floor=floor, direction=direction)
    assert core.effect_verdict(result, 1.0) is _AS_COMPARED[result.verdict]


@given(
    mine=SAMPLES,
    theirs=SAMPLES,
    floor=FLOORS,
    direction=DIRECTIONS,
    bars=st.tuples(BARS, BARS),
)
def test_a_harder_bar_never_turns_a_worse_verdict_into_a_better_one(
    mine: list[float],
    theirs: list[float],
    floor: float,
    direction: Direction,
    bars: tuple[float, float],
) -> None:
    """For a max metric a higher bar is harder; for a min metric a lower one is."""
    easy, hard = (min(bars), max(bars))
    if direction == "min":
        easy, hard = hard, easy
    result = compared(mine, theirs, floor=floor, direction=direction)
    assert _RANK[core.effect_verdict(result, hard)] <= _RANK[core.effect_verdict(result, easy)]


@given(mine=SAMPLES, theirs=SAMPLES, floor=FLOORS, bar=BARS)
def test_flipping_the_direction_and_the_side_of_the_bar_flips_nothing_but_the_metric(
    mine: list[float], theirs: list[float], floor: float, bar: float
) -> None:
    """A min metric with ratio r and bar b says what a max metric with 2 - r and 2 - b does."""
    result = compared(mine, theirs, floor=floor, direction="max")
    mirrored = result.model_copy(update={"direction": "min", "ratio": 2 - result.ratio})
    assert core.effect_verdict(mirrored, 2 - bar) is core.effect_verdict(result, bar)


@given(mine=SAMPLES, theirs=SAMPLES, floor=FLOORS, direction=DIRECTIONS)
def test_a_result_exactly_at_the_bar_is_within_noise(
    mine: list[float], theirs: list[float], floor: float, direction: Direction
) -> None:
    result = compared(mine, theirs, floor=floor, direction=direction)
    assert core.effect_verdict(result, result.ratio) is core.EffectVerdict.WITHIN_NOISE


@given(mine=SAMPLES, theirs=SAMPLES, floor=FLOORS, direction=DIRECTIONS, bar=BARS)
def test_more_noise_never_turns_an_undecided_result_into_a_verdict(
    mine: list[float], theirs: list[float], floor: float, direction: Direction, bar: float
) -> None:
    result = compared(mine, theirs, floor=floor, direction=direction)
    noisier = result.model_copy(update={"noise": result.noise * 2 + 0.01})
    if core.effect_verdict(result, bar) is core.EffectVerdict.WITHIN_NOISE:
        assert core.effect_verdict(noisier, bar) is core.EffectVerdict.WITHIN_NOISE
