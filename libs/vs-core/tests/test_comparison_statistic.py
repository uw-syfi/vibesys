"""The statistic behind a comparison: what a verdict is allowed to depend on (#3047).

A comparison is of the candidate's samples with the base's. These properties hold for any
samples: the order they were taken in changes nothing, a unit change (scaling every value)
changes nothing, flipping which way is better flips better and worse, more noise never turns
a within-noise result into a verdict, and the noise never falls below what the task declares
for the samples it has. A task that declares no noise gets a verdict only from runs that
measure it: three a side with a spread, judged by the Student-t quantile, so at equal means a
verdict is about as rare as the 5% the statistic intends.
"""

from __future__ import annotations

import math
import random
from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core

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
    """Among floors a task declares. An undeclared floor (0) is not the bottom of the scale.

    Without a declared floor the noise is estimated from the runs, so a verdict needs three
    runs a side and the wider Student-t bar, where a declared floor uses two noises from one
    run a side (`test_without_a_declared_floor_a_verdict_needs_three_runs_a_side_with_a_spread`).
    """
    low = compared(mine, theirs, floor=floor)
    high = compared(mine, theirs, floor=floor + extra)
    if low.verdict is core.ComparisonVerdict.WITHIN_NOISE and floor > 0:
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


@pytest.mark.regression
@given(mine=VALUES, theirs=VALUES, direction=st.sampled_from(("max", "min")))
@example(mine=100.3, theirs=100.0, direction="max")
@example(mine=50.0, theirs=100.0, direction="min")
def test_one_run_a_side_and_no_declared_floor_decide_nothing(
    mine: float, theirs: float, direction: Literal["max", "min"]
) -> None:
    """With no declared ``relative_noise`` and one run a side, nothing measured the noise.

    Before, the noise was taken to be zero, so a 0.3% difference between two single runs was
    ``better``, and only an exact tie was a close call, so no second sample was ever taken.
    Now the comparison gives no verdict for any gain and is a close call, which earns the base
    and the official check the samples that measure the noise.
    """
    result = compared([mine], [theirs], direction=direction)
    assert result.verdict is core.ComparisonVerdict.WITHIN_NOISE
    assert core.close_call(result, UNDECLARED)


UNDECLARED = core.ComparisonPolicy()
"""The policy of a task that declares no ``relative_noise``."""
MIN = core.MIN_SAMPLES_WITHOUT_FLOOR


@pytest.mark.regression
@given(
    mine=st.lists(VALUES, min_size=1, max_size=6),
    theirs=st.lists(VALUES, min_size=1, max_size=6),
    gain=st.floats(min_value=0.5, max_value=2.0),
)
@example(mine=[100.0, 100.0], theirs=[50.0], gain=1.0)
@example(mine=[100.3, 100.3], theirs=[100.0, 100.0], gain=1.0)
@example(mine=[100.3, 100.2, 100.4], theirs=[100.0, 100.0], gain=1.0)
def test_without_a_declared_floor_a_verdict_needs_three_runs_a_side_with_a_spread(
    mine: list[float], theirs: list[float], gain: float
) -> None:
    """With no declared floor, fewer than three runs a side decide nothing, whatever they read.

    Before, one more base run (two on one side, one degree of freedom) was enough: when it read
    what the first did, the spread was zero and a 0.3% gap was ``better`` again, and even a
    nonzero spread from one degree of freedom judged by two noises gives a verdict at equal
    means about 30% of the time. Such a comparison stays a close call, so it earns samples.
    """
    result = compared([v * gain for v in mine], theirs)
    if min(len(mine), len(theirs)) < MIN:
        assert result.verdict is core.ComparisonVerdict.WITHIN_NOISE
        assert core.close_call(result, UNDECLARED)


@pytest.mark.regression
@given(
    value=VALUES,
    other=VALUES,
    m=st.integers(min_value=1, max_value=8),
    n=st.integers(min_value=1, max_value=8),
    direction=st.sampled_from(("max", "min")),
)
@example(value=100.3, other=100.0, m=1, n=2, direction="max")
@example(value=101.0, other=100.0, m=MIN, n=MIN, direction="max")
@example(value=50.0, other=100.0, m=8, n=8, direction="min")
def test_identical_runs_without_a_declared_floor_never_give_a_verdict(
    value: float, other: float, m: int, n: int, direction: Literal["max", "min"]
) -> None:
    """Runs that all read alike show no noise; without a declared floor that is no estimate.

    A quantized, cached or deterministic benchmark reads the same every time. Before, the
    second base run of such a benchmark gave a spread of zero, so a zero noise, and the sign
    of the difference became the verdict.
    """
    result = compared([value] * m, [other] * n, direction=direction)
    assert result.verdict is core.ComparisonVerdict.WITHIN_NOISE
    assert core.close_call(result, UNDECLARED)


T_975_AT_4 = 2.776
"""The two-sided 95% Student-t quantile for 4 degrees of freedom (three runs a side)."""


def test_three_runs_a_side_with_a_spread_give_a_verdict_at_the_student_t_bar() -> None:
    """Three runs a side, no declared floor: the noise is the pooled spread, the bar ``t(4)``.

    Candidate 101, 99, 100 and base 100, 102, 98 have relative deviations whose squares sum to
    0.001 over 4 degrees of freedom, so the noise is ``sqrt(0.001 / 4) * sqrt(2 / 3)`` at a
    ratio of one. A gain just above ``t(4)`` noises is ``better``; one between two noises and
    ``t(4)`` noises is not, because the spread was estimated from those six runs.
    """
    noise = math.sqrt(0.001 / 4) * math.sqrt(2 / 3)
    level = compared([101.0, 99.0, 100.0], [100.0, 102.0, 98.0])
    assert math.isclose(level.noise, noise, rel_tol=1e-9)
    assert level.verdict is core.ComparisonVerdict.WITHIN_NOISE
    # Scaling the candidate by r keeps the spread and scales the noise by r, so a gain of
    # k noises is the ratio r with r - 1 = k * r * noise.
    for factor, verdict in (
        (1 / (1 - 1.01 * T_975_AT_4 * noise), core.ComparisonVerdict.BETTER),
        (1 / (1 - 0.99 * T_975_AT_4 * noise), core.ComparisonVerdict.WITHIN_NOISE),
        (1 / (1 - 2.5 * noise), core.ComparisonVerdict.WITHIN_NOISE),
        (1 / (1 + 1.01 * T_975_AT_4 * noise), core.ComparisonVerdict.WORSE),
    ):
        result = compared([101.0 * factor, 99.0 * factor, 100.0 * factor], [100.0, 102.0, 98.0])
        assert result.verdict is verdict, factor
        assert core.close_call(result, UNDECLARED) is (
            verdict is core.ComparisonVerdict.WITHIN_NOISE
        )


SIMULATED = 4000
"""Comparisons simulated per sample count: enough that 7% is several standard errors above 5%."""


@pytest.mark.regression
@pytest.mark.parametrize(("m", "n"), [(3, 3), (3, 5), (4, 4), (6, 6), (1, 2), (2, 2)])
def test_at_equal_means_a_verdict_without_a_declared_floor_is_rare(m: int, n: int) -> None:
    """At equal true means, at most about 5% of comparisons give a verdict (seeded simulation).

    Every run reads 100 times one plus a normal relative noise of 2%, on both sides, so any
    verdict is false. Before, two runs on one side and one on the other (one degree of
    freedom) judged by two noises gave a false verdict about 30% of the time. Fewer than three
    runs a side now give none, and three or more give about 5%: at most 7% here.
    """
    draw = random.Random(f"{m}:{n}")  # noqa: S311  # lint-waiver: a seeded simulation, not a secret.
    verdicts = 0
    for _ in range(SIMULATED):
        mine = [100.0 * (1 + draw.gauss(0.0, 0.02)) for _ in range(m)]
        theirs = [100.0 * (1 + draw.gauss(0.0, 0.02)) for _ in range(n)]
        if compared(mine, theirs).verdict is not core.ComparisonVerdict.WITHIN_NOISE:
            verdicts += 1
    rate = verdicts / SIMULATED
    if min(m, n) < MIN:
        assert rate == 0
    else:
        assert rate <= 0.07, rate
