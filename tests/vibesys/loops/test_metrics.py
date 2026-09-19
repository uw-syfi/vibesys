"""Tests for the metric space shared by the optimization loops."""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError

from vibesys.loops.metrics import Measurement, MetricComparison, MetricSpace, Objective

_OPS = Objective(name="ops", direction="max")
_LATENCY = Objective(name="latency", direction="min")


def _reading(value: float, metric: str = "ops") -> Measurement:
    return Measurement(metric=metric, value=value)


def test_relative_noise_is_validated_to_the_configurable_range() -> None:
    assert MetricSpace().relative_noise == 0.0
    assert MetricSpace(relative_noise=0.05).relative_noise == 0.05
    for invalid in (-0.01, 1.0, 1.5):
        with pytest.raises(ValidationError):
            MetricSpace(relative_noise=invalid)


def test_the_space_is_frozen_and_its_axes_are_unique() -> None:
    assert MetricSpace.model_config["frozen"] is True
    with pytest.raises(ValidationError):
        MetricSpace(objectives=(_OPS, Objective(name="ops", direction="min")))


def test_the_axis_supplies_the_direction_a_reading_omits() -> None:
    space = MetricSpace(objectives=(_OPS,))

    assert space.direction(_reading(1.0)) == "max"
    # A reading measured before its axis was configured keeps its own meaning.
    assert space.direction(Measurement(metric="ops", value=1.0, direction="min")) == "min"
    assert space.direction(_reading(1.0, "unknown")) is None
    assert space.direction(None) is None


def test_signed_primary_orients_headline_values_for_ranking() -> None:
    maximizing = MetricSpace(objectives=(_OPS,))
    minimizing = MetricSpace(objectives=(_LATENCY,))

    assert maximizing.signed_primary(12.0) == 12.0
    assert minimizing.signed_primary(12.0) == -12.0
    assert MetricSpace().signed_primary(12.0) == 12.0


@pytest.mark.parametrize("direction", ["max", "min"])
def test_a_delta_exactly_at_the_tolerance_is_within_noise(
    direction: Literal["max", "min"],
) -> None:
    """The tolerance is inclusive: only a delta beyond it is a real result."""
    space = MetricSpace(
        objectives=(Objective(name="ops", direction=direction),), relative_noise=0.05
    )

    assert space.compare(_reading(105.0), _reading(100.0)) is MetricComparison.WITHIN_NOISE
    assert space.compare(_reading(95.0), _reading(100.0)) is MetricComparison.WITHIN_NOISE
    beyond = MetricComparison.BETTER if direction == "max" else MetricComparison.WORSE
    assert space.compare(_reading(105.01), _reading(100.0)) is beyond


@pytest.mark.parametrize("direction", ["max", "min"])
def test_direction_decides_which_side_of_the_baseline_is_better(
    direction: Literal["max", "min"],
) -> None:
    space = MetricSpace(
        objectives=(Objective(name="ops", direction=direction),), relative_noise=0.05
    )

    higher = space.compare(_reading(120.0), _reading(100.0))
    lower = space.compare(_reading(80.0), _reading(100.0))

    assert higher is (MetricComparison.BETTER if direction == "max" else MetricComparison.WORSE)
    assert lower is (MetricComparison.WORSE if direction == "max" else MetricComparison.BETTER)


def test_a_zero_noise_space_compares_strictly() -> None:
    space = MetricSpace(objectives=(_OPS,))

    assert space.compare(_reading(100.000001), _reading(100.0)) is MetricComparison.BETTER
    assert space.compare(_reading(99.999999), _reading(100.0)) is MetricComparison.WORSE
    assert space.compare(_reading(100.0), _reading(100.0)) is MetricComparison.WITHIN_NOISE


def test_a_zero_baseline_leaves_no_scale_so_the_comparison_is_strict() -> None:
    """The tolerance is relative, so at a zero baseline it is zero too."""
    space = MetricSpace(objectives=(_OPS,), relative_noise=0.5)

    assert space.compare(_reading(0.5), _reading(0.0)) is MetricComparison.BETTER
    assert space.compare(_reading(-0.5), _reading(0.0)) is MetricComparison.WORSE
    assert space.compare(_reading(0.0), _reading(0.0)) is MetricComparison.WITHIN_NOISE


def test_a_negative_baseline_scales_the_tolerance_by_its_magnitude() -> None:
    space = MetricSpace(objectives=(_OPS,), relative_noise=0.1)
    minimizing = MetricSpace(
        objectives=(Objective(name="ops", direction="min"),), relative_noise=0.1
    )

    assert space.compare(_reading(-95.0), _reading(-100.0)) is MetricComparison.WITHIN_NOISE
    assert space.compare(_reading(-85.0), _reading(-100.0)) is MetricComparison.BETTER
    assert space.compare(_reading(-115.0), _reading(-100.0)) is MetricComparison.WORSE
    assert minimizing.compare(_reading(-85.0), _reading(-100.0)) is MetricComparison.WORSE


@pytest.mark.parametrize(
    ("candidate", "baseline"),
    [
        (None, _reading(100.0)),
        (_reading(100.0), None),
        (_reading(100.0, "unconfigured"), _reading(100.0, "unconfigured")),
        (_reading(100.0), _reading(100.0, "latency")),
    ],
)
def test_missing_or_mismatched_evidence_is_incomparable(
    candidate: Measurement | None,
    baseline: Measurement | None,
) -> None:
    space = MetricSpace(objectives=(_OPS,), relative_noise=0.05)

    assert space.compare(candidate, baseline) is MetricComparison.INCOMPARABLE


def test_best_and_compare_to_best_order_a_scalar_history() -> None:
    space = MetricSpace(objectives=(_OPS,), relative_noise=0.05)
    history = [_reading(100.0), _reading(90.0), _reading(99.0)]

    assert space.best(history, lambda item: item) == _reading(100.0)
    assert space.best([], lambda item: item) is None
    # Ties, including ties within the tolerance, keep the earlier member, so
    # callers order their own tie-break.
    assert space.best([_reading(100.0, "a"), _reading(103.0, "a")], _named_a) == _reading(
        100.0, "a"
    )
    # An empty history is advanced by anything measurable.
    assert space.compare_to_best(_reading(1.0), []) is MetricComparison.BETTER
    assert space.compare_to_best(None, history) is MetricComparison.INCOMPARABLE
    assert space.compare_to_best(_reading(1.0, "unconfigured"), []) is MetricComparison.INCOMPARABLE
    assert space.compare_to_best(_reading(104.0), history) is MetricComparison.WITHIN_NOISE
    assert space.compare_to_best(_reading(120.0), history) is MetricComparison.BETTER


def _named_a(item: Measurement) -> Measurement:
    """Read every member on one axis so the space can order them."""
    return Measurement(metric="ops", value=item.value)


def test_dominance_needs_every_axis_and_one_material_win() -> None:
    space = MetricSpace(objectives=(_OPS, _LATENCY), relative_noise=0.03)
    baseline = {"ops": 100.0, "latency": 100.0}

    assert space.dominates({"ops": 110.0, "latency": 101.0}, baseline)
    # Better on one axis, materially worse on the other.
    assert not space.dominates({"ops": 110.0, "latency": 110.0}, baseline)
    # Everything within noise is not a dominating point.
    assert not space.dominates({"ops": 102.0, "latency": 101.0}, baseline)
    # A missing axis makes the rows incomparable rather than dominated.
    assert not space.dominates({"ops": 110.0}, baseline)
    assert not space.dominates({"ops": 110.0, "latency": 90.0}, {"ops": 100.0})


def test_a_space_with_no_axes_dominates_nothing() -> None:
    assert not MetricSpace().dominates({"ops": 110.0}, {"ops": 100.0})
    assert not MetricSpace().complete({"ops": 110.0})


def test_the_frontier_keeps_every_tradeoff_and_drops_dominated_points() -> None:
    space = MetricSpace(objectives=(_OPS, _LATENCY))
    rows = [
        {"ops": 100.0, "latency": 80.0},
        {"ops": 140.0, "latency": 100.0},
        {"ops": 90.0, "latency": 110.0},
    ]

    assert space.frontier(rows, lambda row: row) == rows[:2]


def test_completeness_requires_a_value_on_every_configured_axis() -> None:
    space = MetricSpace(objectives=(_OPS, _LATENCY))

    assert space.complete({"ops": 1.0, "latency": 2.0, "extra": 3.0})
    assert not space.complete({"ops": 1.0})
