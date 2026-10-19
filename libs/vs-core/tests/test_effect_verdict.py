"""The framework's verdict on a plan's expected effect, derived from a comparison (#3047).

A plan states a typed bar, a minimum ratio over the base in the metric's own direction. What the
measured comparison says of it is derived, never declared: `supported` when the candidate clears
the bar by more than a critical number of noises (two under a declared floor), `disproven` when
it misses by more than that, and `within_noise` otherwise. A comparison with no estimate of its
noise (no declared floor and fewer than three runs a side, or runs that show none) is
`inconclusive` of any bar. These properties hold for any comparison, any floor and any bar.
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
    core.EffectVerdict.INCONCLUSIVE: 1,
    core.EffectVerdict.DISPROVEN: 0,
}
_AS_COMPARED = {
    core.ComparisonVerdict.BETTER: core.EffectVerdict.SUPPORTED,
    core.ComparisonVerdict.WITHIN_NOISE: core.EffectVerdict.WITHIN_NOISE,
    core.ComparisonVerdict.WORSE: core.EffectVerdict.DISPROVEN,
    core.ComparisonVerdict.INCONCLUSIVE: core.EffectVerdict.INCONCLUSIVE,
}


def policy(floor: float) -> core.ComparisonPolicy:
    return core.ComparisonPolicy(noise_floor=floor)


@given(mine=SAMPLES, theirs=SAMPLES, floor=FLOORS, direction=DIRECTIONS)
def test_a_bar_of_one_is_the_comparisons_own_verdict_or_more_cautious(
    mine: list[float], theirs: list[float], floor: float, direction: Direction
) -> None:
    """Exactly the verdict under a declared floor; without one, never a firmer call than it.

    Without a floor the stored comparison does not carry Welch's degrees of freedom, so the
    effect takes the critical number at the fewest degrees it can have, never below the one the
    verdict used.
    """
    result = compared(mine, theirs, floor=floor, direction=direction)
    effect = core.effect_verdict(result, 1.0, policy(floor))
    if floor > 0:
        assert effect is _AS_COMPARED[result.verdict]
    else:
        assert effect in {
            _AS_COMPARED[result.verdict],
            core.EffectVerdict.WITHIN_NOISE,
            core.EffectVerdict.INCONCLUSIVE,
        }


@given(
    mine=st.lists(VALUES, min_size=1, max_size=core.MIN_SAMPLES_WITHOUT_FLOOR - 1),
    theirs=SAMPLES,
    direction=DIRECTIONS,
    bar=BARS,
)
def test_without_a_declared_floor_too_few_runs_say_nothing_of_any_bar(
    mine: list[float], theirs: list[float], direction: Direction, bar: float
) -> None:
    """Fewer than three candidate runs are no estimate of its noise, so no bar is cleared or missed."""
    for one, other in ((mine, theirs), (theirs, mine)):
        result = compared(one, other, direction=direction)
        assert core.effect_verdict(result, bar, policy(0.0)) is core.EffectVerdict.INCONCLUSIVE


@given(value=VALUES, base=VALUES, direction=DIRECTIONS, bar=BARS)
def test_without_a_declared_floor_identical_runs_say_nothing_of_any_bar(
    value: float, base: float, direction: Direction, bar: float
) -> None:
    """Three identical runs a side show no noise, which is no measurement that there is none."""
    runs = core.MIN_SAMPLES_WITHOUT_FLOOR
    result = compared([value] * runs, [base] * runs, direction=direction)
    assert core.effect_verdict(result, bar, policy(0.0)) is core.EffectVerdict.INCONCLUSIVE


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
    assert (
        _RANK[core.effect_verdict(result, hard, policy(floor))]
        <= _RANK[core.effect_verdict(result, easy, policy(floor))]
    )


@given(mine=SAMPLES, theirs=SAMPLES, floor=FLOORS, bar=BARS)
def test_flipping_the_direction_and_the_side_of_the_bar_flips_nothing_but_the_metric(
    mine: list[float], theirs: list[float], floor: float, bar: float
) -> None:
    """A min metric with ratio r and bar b says what a max metric with 2 - r and 2 - b does."""
    result = compared(mine, theirs, floor=floor, direction="max")
    mirrored = result.model_copy(update={"direction": "min", "ratio": 2 - result.ratio})
    assert core.effect_verdict(mirrored, 2 - bar, policy(floor)) is core.effect_verdict(
        result, bar, policy(floor)
    )


@given(mine=SAMPLES, theirs=SAMPLES, floor=FLOORS, direction=DIRECTIONS)
def test_a_result_exactly_at_the_bar_is_within_noise(
    mine: list[float], theirs: list[float], floor: float, direction: Direction
) -> None:
    result = compared(mine, theirs, floor=floor, direction=direction)
    called = core.effect_resolution(result, policy(floor)) is not None
    assert core.effect_verdict(result, result.ratio, policy(floor)) is (
        core.EffectVerdict.WITHIN_NOISE if called else core.EffectVerdict.INCONCLUSIVE
    )


@given(mine=SAMPLES, theirs=SAMPLES, floor=FLOORS, direction=DIRECTIONS, bar=BARS)
def test_more_noise_never_turns_an_undecided_result_into_a_verdict(
    mine: list[float], theirs: list[float], floor: float, direction: Direction, bar: float
) -> None:
    result = compared(mine, theirs, floor=floor, direction=direction)
    noisier = result.model_copy(update={"noise": result.noise * 2 + 0.01})
    if core.effect_verdict(result, bar, policy(floor)) is core.EffectVerdict.WITHIN_NOISE:
        assert core.effect_verdict(noisier, bar, policy(floor)) is core.EffectVerdict.WITHIN_NOISE


@given(mine=SAMPLES, theirs=SAMPLES, floor=FLOORS, direction=DIRECTIONS, bar=BARS)
def test_the_resolution_is_the_distance_from_a_bar_at_which_the_verdict_changes(
    mine: list[float], theirs: list[float], floor: float, direction: Direction, bar: float
) -> None:
    """Beyond the resolution the effect is called, within it never; no resolution, never."""
    result = compared(mine, theirs, floor=floor, direction=direction)
    resolution = core.effect_resolution(result, policy(floor))
    verdict = core.effect_verdict(result, bar, policy(floor))
    if resolution is None:
        assert verdict is core.EffectVerdict.INCONCLUSIVE
        return
    assert resolution >= 0
    margin = (1.0 if direction == "max" else -1.0) * (result.ratio - bar)
    expected = (
        core.EffectVerdict.SUPPORTED
        if margin > resolution
        else core.EffectVerdict.DISPROVEN
        if margin < -resolution
        else core.EffectVerdict.WITHIN_NOISE
    )
    assert verdict is expected


@given(mine=SAMPLES, theirs=SAMPLES, direction=DIRECTIONS)
def test_under_a_declared_floor_the_resolution_is_the_verdict_noises_of_the_comparisons_noise(
    mine: list[float], theirs: list[float], direction: Direction
) -> None:
    result = compared(mine, theirs, floor=0.02, direction=direction)
    resolution = core.effect_resolution(result, policy(0.02))
    assert resolution == core.VERDICT_NOISES * result.noise
