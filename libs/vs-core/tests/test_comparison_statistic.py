"""The statistic behind a comparison: what a verdict is allowed to depend on (#3047).

A comparison is of the candidate's samples with the base's. These properties hold for any
samples: the order they were taken in changes nothing, a unit change (scaling every value)
changes nothing, flipping which way is better flips better and worse, more noise never turns
a within-noise result into a verdict, and the noise never falls below what the task declares
for the samples it has. A task that declares no noise gets a verdict only from runs that
measure it: three a side with a spread, judged by Welch's t test (each side's own spread, the
Student-t quantile at the floor of the Welch-Satterthwaite degrees of freedom), so at equal
means a verdict is about as rare as the 5% the statistic intends, whatever the two spreads and
counts.
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
"""Any floor a task may carry. 0 declares nothing, so it is the other estimator (`DECLARED_FLOORS`)."""
DECLARED_FLOORS = st.floats(min_value=0.0, max_value=0.2, exclude_min=True, allow_nan=False)
"""A floor a task declares: noise is pooled and bounded below by it, never from the runs alone.

Properties that set two floors side by side draw both from here. A floor of 0 does not sit at
the bottom of this scale: without a declared floor the noise is Welch's estimate from each
side's own spread, which neither bounds nor is bounded by any pooled estimate, so the two
cannot be ordered (`test_declaring_a_floor_replaces_the_runs_own_estimate_of_the_noise`).
"""


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
        core.ComparisonVerdict.INCONCLUSIVE: core.ComparisonVerdict.INCONCLUSIVE,
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
    floor=DECLARED_FLOORS,
    extra=st.floats(min_value=0.0, max_value=0.2),
)
def test_a_higher_floor_never_turns_an_undecided_result_into_a_verdict(
    mine: list[float], theirs: list[float], floor: float, extra: float
) -> None:
    """Among floors a task declares: a higher one never lowers the noise or frees a verdict.

    Both floors are declared (`DECLARED_FLOORS`): the noise is the pooled spread bounded below
    by the floor, so it can only grow with it.
    """
    low = compared(mine, theirs, floor=floor)
    high = compared(mine, theirs, floor=floor + extra)
    if low.verdict is core.ComparisonVerdict.WITHIN_NOISE:
        assert high.verdict is core.ComparisonVerdict.WITHIN_NOISE
    assert high.noise >= low.noise - 1e-12


@given(
    mine=st.lists(VALUES, min_size=1, max_size=6),
    theirs=st.lists(VALUES, min_size=1, max_size=6),
    floor=DECLARED_FLOORS,
)
@example(mine=[1.0, 2.0], theirs=[1.0, 1.0, 1.0], floor=0.125)
def test_declaring_a_floor_replaces_the_runs_own_estimate_of_the_noise(
    mine: list[float], theirs: list[float], floor: float
) -> None:
    """A floor of 0 is no floor: its noise is the runs' own, and no declared floor bounds it.

    The nightly found a higher floor lowering the noise: candidate 1, 2 against base 1, 1, 1
    at floor 0 (nothing declared) has Welch's 0.5 from the two runs' own spread, and at a
    floor of 0.125 the declared pooled estimate is 0.373. Neither is wrong: a task that
    declares a floor has stated its noise and gets a verdict from one run a side, where one that
    declares none gets one only from the runs it measures. So a declared floor with the same
    samples is a verdict-giving comparison, and the undeclared one is `INCONCLUSIVE` unless it
    holds three runs a side with a spread, which is no ordering of the two.
    """
    declared = compared(mine, theirs, floor=floor)
    undeclared = compared(mine, theirs, floor=0.0)
    assert declared.verdict is not core.ComparisonVerdict.INCONCLUSIVE
    if min(len(mine), len(theirs)) < MIN:
        assert undeclared.verdict is core.ComparisonVerdict.INCONCLUSIVE


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
    assert result.verdict is core.ComparisonVerdict.INCONCLUSIVE
    assert core.close_call(result)


MIN = core.MIN_SAMPLES_WITHOUT_FLOOR
_CALLED = (core.ComparisonVerdict.BETTER, core.ComparisonVerdict.WORSE)


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
        assert result.verdict is core.ComparisonVerdict.INCONCLUSIVE
        assert core.close_call(result)


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
    assert result.verdict is core.ComparisonVerdict.INCONCLUSIVE
    assert core.close_call(result)


T_975_AT_4 = 2.776
"""The two-sided 95% Student-t quantile for 4 degrees of freedom (three runs a side, equal spreads)."""
T_975_AT_2 = 4.303
"""The two-sided 95% Student-t quantile for 2 degrees of freedom."""


def scaled_to(level: core.Comparison, noises: float) -> float:
    """The factor on the candidate's runs that puts its gain at ``noises`` of its own noise.

    Scaling the candidate by ``f`` keeps each side's relative spread, so the noise is
    ``f * level.noise`` and the ratio ``f * level.ratio``; the gain ``f * r - 1`` is ``k`` noises
    when ``f * r * (1 - k * e) = 1``, with ``e`` the noise over the ratio.
    """
    relative = level.noise / level.ratio
    return 1 / (level.ratio * (1 - noises * relative))


@pytest.mark.regression
def test_three_runs_a_side_with_a_spread_give_a_verdict_at_welchs_bar() -> None:
    """Three runs a side, no declared floor: each side's own spread, the bar at Welch's degrees.

    Candidate 101, 99, 100 has relative variance 0.0001 and base 100, 102, 98 has 0.0004, so
    the noise is ``sqrt(0.0001 / 3 + 0.0004 / 3)`` at a ratio of one, and the Welch-Satterthwaite
    degrees of freedom are 2.94, whose floor 2 gives the bar ``t(2)`` = 4.303. Before, the pooled
    spread took 4 degrees of freedom and the bar 2.776, so a gain of 3 noises was ``better``
    although the base's runs vary four times as much as the candidate's. The same candidate
    against a base that varies as it does has 4 degrees of freedom and the bar ``t(4)``.
    """
    candidate, base = [101.0, 99.0, 100.0], [100.0, 102.0, 98.0]
    level = compared(candidate, base)
    assert math.isclose(level.noise, math.sqrt(0.0001 / 3 + 0.0004 / 3), rel_tol=1e-9)
    assert level.verdict is core.ComparisonVerdict.WITHIN_NOISE
    alike = compared(candidate, [101.0, 99.0, 100.0])
    for start, bar, between in ((level, T_975_AT_2, 3.0), (alike, T_975_AT_4, 2.5)):
        theirs = base if start is level else [101.0, 99.0, 100.0]
        for noises, verdict in (
            (1.01 * bar, core.ComparisonVerdict.BETTER),
            (0.99 * bar, core.ComparisonVerdict.WITHIN_NOISE),
            (between, core.ComparisonVerdict.WITHIN_NOISE),
            (-1.01 * bar, core.ComparisonVerdict.WORSE),
        ):
            factor = scaled_to(start, noises)
            result = compared([v * factor for v in candidate], theirs)
            assert result.verdict is verdict, (bar, noises)
            assert core.close_call(result) is (verdict is core.ComparisonVerdict.WITHIN_NOISE)


@pytest.mark.regression
def test_one_outlier_run_of_the_base_keeps_a_large_loss_within_noise() -> None:
    """A live run read a candidate at 0.66 of its base, with noise 0.22, and called it within noise.

    The base's three runs read 2.69M, 2.28M and 6.01M ops/s on a shared host. Their relative
    spread (0.56) makes the noise of the ratio 0.22 and leaves 2 degrees of freedom, so the bar
    is ``t(2)`` = 4.303 noises: a loss of 0.34 is 1.6 noises. The call is the statistic's, not a
    fault: against a base of the same mean without the spread the same candidate is ``worse``.
    """
    candidate = [2_562_962.8, 2_573_260.6, 2_109_707.7]
    noisy = compared(candidate, [2_688_992.7, 2_275_733.2, 6_013_612.1])
    assert (round(noisy.ratio, 2), round(noisy.noise, 2)) == (0.66, 0.22)
    assert noisy.verdict is core.ComparisonVerdict.WITHIN_NOISE
    assert (1 - noisy.ratio) < T_975_AT_2 * noisy.noise
    steady = compared(candidate, [3_500_000.0, 3_660_000.0, 3_820_000.0])
    assert round(steady.ratio, 2) == 0.66
    assert steady.verdict is core.ComparisonVerdict.WORSE


def student_t_quantile(degrees: int) -> float:
    """The two-sided 95% Student-t quantile, from the density integrated numerically.

    Independent of the statistic's table: Simpson's rule over the density from 0, and bisection
    for the point where the distribution reaches 0.975.
    """
    scale = math.exp(math.lgamma((degrees + 1) / 2) - math.lgamma(degrees / 2))
    scale /= math.sqrt(degrees * math.pi)

    def density(x: float) -> float:
        return scale * (1 + x * x / degrees) ** (-(degrees + 1) / 2)

    def below(q: float) -> float:
        steps = 2000
        width = q / steps
        inner = sum((4 if i % 2 else 2) * density(i * width) for i in range(1, steps))
        return 0.5 + width / 3 * (density(0.0) + inner + density(q))

    low, high = 1.0, 13.0
    for _ in range(60):
        middle = (low + high) / 2
        low, high = (middle, high) if below(middle) < 0.975 else (low, middle)
    return (low + high) / 2


TABLE_LAST = 30
"""Beyond 30 degrees of freedom the statistic keeps the quantile at 30, slightly conservative."""


@pytest.mark.regression
@pytest.mark.parametrize("degrees", [*range(2, TABLE_LAST + 1), 31, 45])
def test_the_bar_without_a_declared_floor_is_the_student_t_quantile_at_each_degree(
    degrees: int,
) -> None:
    """At every degree of freedom a verdict can have, the bar is the t quantile to 3 decimals.

    ``degrees + 1`` candidate runs with a spread against three identical base runs: the base
    side contributes no variance, so Welch's degrees of freedom are exactly the candidate's
    runs less one. A gain 0.0004 noises above the quantile (rounded to the table's three
    decimals) is ``better`` and one 0.0004 below is not, so changing any entry of the table, or
    the entry used beyond it, is caught.
    """
    candidate = [100.0 + (i - degrees / 2) for i in range(degrees + 1)]
    level = compared(candidate, [100.0, 100.0, 100.0])
    bar = round(student_t_quantile(min(degrees, TABLE_LAST)), 3)
    for noises, verdict in (
        (bar + 0.0004, core.ComparisonVerdict.BETTER),
        (bar - 0.0004, core.ComparisonVerdict.WITHIN_NOISE),
    ):
        factor = scaled_to(level, noises)
        result = compared([v * factor for v in candidate], [100.0, 100.0, 100.0])
        assert result.verdict is verdict, (degrees, bar, noises)


SIMULATED = 4000
"""Comparisons simulated per case: enough that 7% is several standard errors above 5%."""


@pytest.mark.regression
@pytest.mark.parametrize(
    ("m", "n", "spread_mine", "spread_theirs"),
    [
        (3, 3, 0.02, 0.02),
        (3, 5, 0.02, 0.02),
        (4, 4, 0.02, 0.02),
        (6, 6, 0.02, 0.02),
        (10, 3, 0.02, 0.02),
        (3, 3, 0.01, 0.04),
        (10, 3, 0.01, 0.02),
        (10, 3, 0.01, 0.04),
        (30, 3, 0.01, 0.04),
        (30, 3, 0.005, 0.04),
        (3, 6, 0.04, 0.01),
        (1, 2, 0.02, 0.02),
        (2, 2, 0.02, 0.02),
    ],
)
def test_at_equal_means_a_verdict_without_a_declared_floor_is_rare(
    m: int, n: int, spread_mine: float, spread_theirs: float
) -> None:
    """At equal true means, at most about 5% of comparisons give a verdict (seeded simulation).

    Every candidate run reads 100 times one plus a normal relative noise of ``spread_mine``,
    every base run likewise with ``spread_theirs``, so any verdict is false. Before, a pooled
    spread assumed both sides vary alike: with many quiet candidate runs against three noisy
    base runs it understated the base's noise, and 10 + 3 runs at 1% / 4% gave a false verdict
    about 30% of the time (30 + 3 at 0.5% / 4%: about 59%). Fewer than three runs a side give
    none, and three or more give at most 7% here, whatever the spreads and counts.
    """
    draw = random.Random(f"{m}:{n}:{spread_mine}:{spread_theirs}")  # noqa: S311  # LW-994811; seeded simulation needs reproducible non-cryptographic draws.
    # > System entropy would make the regression nondeterministic; a secure generator adds no
    # > test guarantee and does not provide the Gaussian draws this simulation needs.
    verdicts = 0
    for _ in range(SIMULATED):
        mine = [100.0 * (1 + draw.gauss(0.0, spread_mine)) for _ in range(m)]
        theirs = [100.0 * (1 + draw.gauss(0.0, spread_theirs)) for _ in range(n)]
        if compared(mine, theirs).verdict in _CALLED:
            verdicts += 1
    rate = verdicts / SIMULATED
    if min(m, n) < MIN:
        assert rate == 0
    else:
        assert rate <= 0.07, rate


SMALL_SPREAD = st.floats(min_value=0.0, max_value=0.005)
EFFECTS = st.one_of(
    st.floats(min_value=1.2, max_value=3.0), st.floats(min_value=0.3, max_value=0.8)
)


def _runs(centre: float, spreads: list[float]) -> list[float]:
    """Runs reading ``centre`` times one plus a relative deviation of at most the spread."""
    return [centre * (1 + s * (-1) ** i) for i, s in enumerate(spreads)]


@pytest.mark.regression
@given(
    base=st.lists(SMALL_SPREAD, min_size=1, max_size=6),
    mine=st.lists(SMALL_SPREAD, min_size=1, max_size=6),
    effect=EFFECTS,
    floor=st.sampled_from((0.0, 0.01)),
    direction=st.sampled_from(("max", "min")),
)
@example(base=[0.005, 0.005], mine=[0.0], effect=1.2371, floor=0.0, direction="max")
def test_a_large_effect_over_a_small_noise_is_never_within_noise(
    base: list[float],
    mine: list[float],
    effect: float,
    floor: float,
    direction: Literal["max", "min"],
) -> None:
    """A live run read 352.37 over one run against a base of 287.03 and 282.66 as ``within_noise``.

    Whatever the run counts, a ratio of at least 1.2 (or at most 0.8) against runs within 0.5%
    of their side's centre is never called noise: with too few runs to estimate the noise the
    verdict is ``inconclusive``, and otherwise it is ``better`` or ``worse`` by the direction.
    """
    result = compared(
        _runs(100.0 * effect, mine), _runs(100.0, base), floor=floor, direction=direction
    )
    assert result.verdict is not core.ComparisonVerdict.WITHIN_NOISE
    known = floor > 0 or (min(len(mine), len(base)) >= MIN and result.noise > 0)
    if not known:
        assert result.verdict is core.ComparisonVerdict.INCONCLUSIVE
        assert core.close_call(result)
    else:
        improves = (effect > 1) == (direction == "max")
        expected = core.ComparisonVerdict.BETTER if improves else core.ComparisonVerdict.WORSE
        assert result.verdict is expected


@given(
    mine=st.lists(VALUES, min_size=1, max_size=6),
    theirs=st.lists(VALUES, min_size=1, max_size=6),
    floor=FLOORS,
    direction=st.sampled_from(("max", "min")),
)
def test_within_noise_only_appears_when_the_effect_is_inside_the_noise_band(
    mine: list[float], theirs: list[float], floor: float, direction: Literal["max", "min"]
) -> None:
    """``within_noise`` is a measured call: the noise is estimated and the gain is inside its band.

    The band is at most 4.303 noises (the widest critical number a verdict can
    use), and under a declared floor exactly ``VERDICT_NOISES``. Without an estimate of the
    noise the verdict is ``inconclusive``, never ``within_noise``.
    """
    result = compared(mine, theirs, floor=floor, direction=direction)
    gain = result.ratio - 1 if direction == "max" else 1 - result.ratio
    if result.verdict is core.ComparisonVerdict.WITHIN_NOISE:
        assert floor > 0 or (min(len(mine), len(theirs)) >= MIN and result.noise > 0)
        widest = core.VERDICT_NOISES if floor > 0 else T_975_AT_2
        assert abs(gain) <= widest * result.noise * (1 + 1e-9)
    elif result.verdict is core.ComparisonVerdict.INCONCLUSIVE:
        assert floor == 0
        assert min(len(mine), len(theirs)) < MIN or result.noise == 0
