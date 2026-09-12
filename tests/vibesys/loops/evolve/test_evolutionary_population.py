"""Unit tests for vibesys.loops.evolve.population.

Pure-logic tests — no agent runner, no _RunContext, no GPU. The
``Population`` and ``Individual`` classes are intentionally free of
runtime imports so this file runs in isolation.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any

import pytest

from vibesys.loops.evolve.population import Individual, Population
from vibesys.loops.metrics import MetricSpace, Objective


def _space(*objectives: Objective, noise: float = 0.0) -> MetricSpace:
    """The metric space a task declaring *objectives* would produce."""
    return MetricSpace(objectives=objectives, relative_noise=noise)


_TPUT_LAT = _space(
    Objective(name="tput", direction="max"),
    Objective(name="lat", direction="min"),
)
_LAT_QUALITY = _space(
    Objective(name="lat", direction="min"),
    Objective(name="quality", direction="max"),
)

# ---------------------------------------------------------------------------
# Population: basic accessors
# ---------------------------------------------------------------------------


def _passed(id_: int, perf: float | None, parent_id: int | None = None, gen: int = 1) -> Individual:
    return Individual(
        id=id_,
        generation=gen,
        parent_id=parent_id,
        commit=f"sha-{id_}",
        perf_metric=perf,
        perf_unit="tok/s",
        passed=True,
        summary=f"individual {id_}",
    )


def _selected_id(selected: Individual | None) -> int:
    """Unwrap a selection that the test requires to be non-None."""
    assert selected is not None
    return selected.id


def _failed(id_: int, parent_id: int | None = None, gen: int = 1) -> Individual:
    return Individual(
        id=id_,
        generation=gen,
        parent_id=parent_id,
        commit=None,
        passed=False,
        summary="failed try",
        feedback="judge said no",
    )


def test_next_id_starts_at_one_and_increments():  # noqa: ANN201  # tracked: #288
    pop = Population()
    assert pop.next_id() == 1
    pop.add(_passed(1, 10.0))
    assert pop.next_id() == 2


def test_passed_filter_excludes_no_commit_and_failed():  # noqa: ANN201  # tracked: #288
    pop = Population([_passed(1, 10.0), _failed(2), _passed(3, 11.0)])
    # Add a synthetic "passed but no commit" — shouldn't be selectable.
    pop.add(Individual(id=4, generation=1, parent_id=None, passed=True, commit=None))
    ids = sorted(i.id for i in pop.passed)
    assert ids == [1, 3]


def test_best_picks_highest_perf_metric():  # noqa: ANN201  # tracked: #288
    pop = Population([_passed(1, 10.0), _passed(2, 12.0), _passed(3, 11.0)])
    assert _selected_id(pop.best(MetricSpace())) == 2


def test_best_returns_none_with_no_passed_individuals():  # noqa: ANN201  # tracked: #288
    pop = Population([_failed(1), _failed(2)])
    assert pop.best(MetricSpace()) is None


def test_best_breaks_ties_by_id():  # noqa: ANN201  # tracked: #288
    pop = Population([_passed(1, 10.0), _passed(2, 10.0)])
    assert _selected_id(pop.best(MetricSpace())) == 2


# ---------------------------------------------------------------------------
# Population.select_parent
# ---------------------------------------------------------------------------


def test_select_parent_empty_returns_none():  # noqa: ANN201  # tracked: #288
    assert Population().select_parent(rng=random.Random(0), space=MetricSpace()) is None  # noqa: S311  # tracked: #288


def test_select_parent_only_failed_returns_none():  # noqa: ANN201  # tracked: #288
    pop = Population([_failed(1), _failed(2)])
    assert pop.select_parent(rng=random.Random(0), space=MetricSpace()) is None  # noqa: S311  # tracked: #288


def test_select_parent_single_passed_returns_it():  # noqa: ANN201  # tracked: #288
    pop = Population([_failed(1), _passed(2, 10.0)])
    assert _selected_id(pop.select_parent(rng=random.Random(0), space=MetricSpace())) == 2  # noqa: S311  # tracked: #288


def test_select_parent_low_temperature_concentrates_on_best():  # noqa: ANN201  # tracked: #288
    """A near-zero temperature should pick the best almost every time."""
    pop = Population([_passed(1, 1.0), _passed(2, 5.0), _passed(3, 10.0)])
    rng = random.Random(123)  # noqa: S311  # tracked: #288
    counts = Counter(
        _selected_id(pop.select_parent(space=MetricSpace(), rng=rng, temperature=0.01))
        for _ in range(200)
    )
    # The best (id=3) should dominate.
    assert counts[3] > 180


def test_select_parent_high_temperature_spreads():  # noqa: ANN201  # tracked: #288
    """High temperature flattens the distribution toward uniform."""
    pop = Population([_passed(1, 1.0), _passed(2, 5.0), _passed(3, 10.0)])
    rng = random.Random(123)  # noqa: S311  # tracked: #288
    counts = Counter(
        _selected_id(pop.select_parent(space=MetricSpace(), rng=rng, temperature=100.0))
        for _ in range(600)
    )
    # All three should be picked a meaningful number of times.
    assert all(counts[i] > 100 for i in (1, 2, 3))


def test_select_parent_uniform_when_all_perfs_equal():  # noqa: ANN201  # tracked: #288
    pop = Population([_passed(1, 7.0), _passed(2, 7.0), _passed(3, 7.0)])
    rng = random.Random(42)  # noqa: S311  # tracked: #288
    counts = Counter(
        _selected_id(pop.select_parent(rng=rng, space=MetricSpace())) for _ in range(300)
    )
    assert all(counts[i] > 50 for i in (1, 2, 3))


# ---------------------------------------------------------------------------
# Population.select_inspirations
# ---------------------------------------------------------------------------


def test_select_inspirations_excludes_parent_and_dedupes():  # noqa: ANN201  # tracked: #288
    pop = Population(
        [_passed(i, float(i)) for i in range(1, 8)]  # ids 1..7, perf 1..7
    )
    rng = random.Random(0)  # noqa: S311  # tracked: #288
    picks = pop.select_inspirations(parent_id=7, k_top=2, k_random=2, rng=rng, space=MetricSpace())
    ids = [i.id for i in picks]
    assert 7 not in ids  # parent excluded
    assert len(set(ids)) == len(ids)  # no dupes


def test_select_inspirations_top_first_then_random():  # noqa: ANN201  # tracked: #288
    pop = Population([_passed(i, float(i)) for i in range(1, 8)])
    rng = random.Random(0)  # noqa: S311  # tracked: #288
    picks = pop.select_inspirations(parent_id=1, k_top=2, k_random=2, rng=rng, space=MetricSpace())
    # First two should be the top-2 highest-perf (ids 7 and 6).
    assert picks[0].id == 7
    assert picks[1].id == 6
    # The remaining slots are random over {2, 3, 4, 5}.
    rest = {p.id for p in picks[2:]}
    assert rest.issubset({2, 3, 4, 5})


def test_select_inspirations_handles_small_population():  # noqa: ANN201  # tracked: #288
    pop = Population([_passed(1, 5.0), _passed(2, 6.0)])
    picks = pop.select_inspirations(
        parent_id=1,
        k_top=2,
        k_random=2,
        rng=random.Random(0),  # noqa: S311  # tracked: #288
        space=MetricSpace(),
    )
    # Only one other passed individual exists; no dupes / no errors.
    assert [p.id for p in picks] == [2]


def test_select_inspirations_empty_when_only_parent_passed():  # noqa: ANN201  # tracked: #288
    pop = Population([_passed(1, 5.0), _failed(2)])
    picks = pop.select_inspirations(
        parent_id=1,
        k_top=3,
        k_random=3,
        rng=random.Random(0),  # noqa: S311  # tracked: #288
        space=MetricSpace(),
    )
    assert picks == []


# ---------------------------------------------------------------------------
# Objective + dominance + Pareto frontier
# ---------------------------------------------------------------------------


def _multi(id_: int, metrics: dict[str, float], parent_id: int | None = None) -> Individual:
    """Helper: build a passed Individual carrying an arbitrary metrics dict.

    `perf_metric` is set to the first metric value so scalar fallbacks
    still work; tests that exercise the frontier override this.
    """
    primary = next(iter(metrics.values())) if metrics else None
    return Individual(
        id=id_,
        generation=1,
        parent_id=parent_id,
        commit=f"sha-{id_}",
        perf_metric=primary,
        perf_unit="primary",
        metrics=dict(metrics),
        passed=True,
        summary=f"individual {id_}",
    )


def test_objective_rejects_unknown_direction():  # noqa: ANN201  # tracked: #288
    # Deliberately outside the declared Literal: the guard is a runtime contract.
    unknown_direction: Any = "bigger"
    with pytest.raises(ValueError):  # noqa: PT011  # tracked: #288
        Objective(name="foo", direction=unknown_direction)


def test_objective_signed_max_passes_through():  # noqa: ANN201  # tracked: #288
    assert Objective("x", "max").signed(5.0) == 5.0


def test_objective_signed_min_negates():  # noqa: ANN201  # tracked: #288
    assert Objective("x", "min").signed(5.0) == -5.0


def test_frontier_returns_only_non_dominated():  # noqa: ANN201  # tracked: #288
    pop = Population(
        [
            _multi(1, {"tput": 100.0, "lat": 80.0}),  # frontier (high tput)
            _multi(2, {"tput": 80.0, "lat": 50.0}),  # frontier (low lat)
            _multi(3, {"tput": 70.0, "lat": 90.0}),  # dominated by id=1 and id=2
            _multi(4, {"tput": 90.0, "lat": 60.0}),  # frontier (middle)
        ]
    )
    front_ids = {i.id for i in pop.frontier(_TPUT_LAT)}
    assert front_ids == {1, 2, 4}


def test_frontier_excludes_individuals_missing_metrics():  # noqa: ANN201  # tracked: #288
    pop = Population(
        [
            _multi(1, {"tput": 100.0, "lat": 80.0}),
            _multi(2, {"tput": 80.0}),  # missing 'lat'
        ]
    )
    front_ids = {i.id for i in pop.frontier(_TPUT_LAT)}
    # Only id=1 is fully metric'd.
    assert front_ids == {1}


def test_frontier_empty_when_no_objectives():  # noqa: ANN201  # tracked: #288
    pop = Population([_multi(1, {"tput": 100.0})])
    assert pop.frontier(MetricSpace()) == []


# ---------------------------------------------------------------------------
# Frontier-biased select_parent / select_inspirations
# ---------------------------------------------------------------------------


def test_select_parent_pareto_mode_draws_from_frontier_with_full_bias():  # noqa: ANN201  # tracked: #288
    """frontier_bias=1.0 → parent always sampled from the Pareto front."""
    pop = Population(
        [
            _multi(1, {"tput": 100.0, "lat": 80.0}),  # frontier
            _multi(2, {"tput": 80.0, "lat": 50.0}),  # frontier
            _multi(3, {"tput": 50.0, "lat": 200.0}),  # dominated
        ]
    )
    rng = random.Random(0)  # noqa: S311  # tracked: #288
    counts = Counter(
        _selected_id(pop.select_parent(rng=rng, space=_TPUT_LAT, frontier_bias=1.0))
        for _ in range(200)
    )
    assert counts[3] == 0  # never the dominated one


def test_select_parent_pareto_mode_falls_back_to_scalar_when_bias_zero():  # noqa: ANN201  # tracked: #288
    """frontier_bias=0.0 → bypasses the frontier branch, scalar softmax used.

    With temperature near 0, the highest perf_metric (id=1, perf=100) wins.
    """
    pop = Population(
        [
            _multi(1, {"tput": 100.0, "lat": 80.0}),
            _multi(2, {"tput": 80.0, "lat": 50.0}),
        ]
    )
    rng = random.Random(0)  # noqa: S311  # tracked: #288
    counts = Counter(
        _selected_id(
            pop.select_parent(
                rng=rng,
                space=_TPUT_LAT,
                frontier_bias=0.0,
                temperature=0.01,
            )
        )
        for _ in range(100)
    )
    assert counts[1] > 90


def test_select_parent_scalar_fallback_respects_min_primary() -> None:
    pop = Population(
        [
            _multi(1, {"lat": 10.0, "quality": 80.0}),
            _multi(2, {"lat": 100.0, "quality": 90.0}),
        ]
    )
    rng = random.Random(0)  # noqa: S311  # deterministic selection test

    counts = Counter(
        _selected_id(
            pop.select_parent(
                rng=rng,
                space=_LAT_QUALITY,
                frontier_bias=0.0,
                temperature=0.01,
            )
        )
        for _ in range(100)
    )

    assert counts[1] > 90


def test_select_parent_falls_back_when_frontier_is_empty():  # noqa: ANN201  # tracked: #288
    """No individual reports both objectives → frontier is empty →
    even with bias=1.0, scalar softmax kicks in so the loop isn't blocked."""
    pop = Population(
        [
            _multi(1, {"tput": 100.0}),  # missing 'lat'
            _multi(2, {"tput": 80.0}),  # missing 'lat'
        ]
    )
    rng = random.Random(0)  # noqa: S311  # tracked: #288
    pick = pop.select_parent(rng=rng, space=_TPUT_LAT, frontier_bias=1.0)
    # We get *some* individual via scalar fallback rather than None.
    assert pick is not None
    assert pick.id in (1, 2)


def test_select_parent_empty_frontier_fallback_respects_min_primary() -> None:
    pop = Population(
        [
            _multi(1, {"lat": 10.0}),
            _multi(2, {"lat": 100.0}),
        ]
    )
    rng = random.Random(0)  # noqa: S311  # deterministic selection test

    counts = Counter(
        _selected_id(
            pop.select_parent(
                rng=rng,
                space=_LAT_QUALITY,
                frontier_bias=1.0,
                temperature=0.01,
            )
        )
        for _ in range(100)
    )

    assert counts[1] > 90


def test_select_inspirations_pareto_mode_pulls_from_frontier_first():  # noqa: ANN201  # tracked: #288
    """Top slots come from the Pareto frontier sorted by primary objective."""
    pop = Population(
        [
            _multi(1, {"tput": 100.0, "lat": 80.0}),  # frontier
            _multi(2, {"tput": 80.0, "lat": 50.0}),  # frontier
            _multi(3, {"tput": 90.0, "lat": 60.0}),  # frontier
            _multi(4, {"tput": 60.0, "lat": 100.0}),  # dominated
            _multi(5, {"tput": 50.0, "lat": 110.0}),  # dominated
        ]
    )
    rng = random.Random(0)  # noqa: S311  # tracked: #288
    picks = pop.select_inspirations(
        parent_id=1,
        k_top=2,
        k_random=1,
        rng=rng,
        space=_TPUT_LAT,
    )
    # First two slots are frontier members (excluding parent #1), sorted by
    # primary objective 'tput' descending: id=3 (tput=90) before id=2 (tput=80).
    assert picks[0].id == 3
    assert picks[1].id == 2
    # Random slot is filled from {4, 5} (non-frontier, non-parent, non-top).
    assert picks[2].id in (4, 5)


def test_select_inspirations_backfills_when_frontier_smaller_than_k_top():  # noqa: ANN201  # tracked: #288
    """When the frontier has only one non-parent member but k_top=3,
    fill the remaining slots from non-frontier passers."""
    pop = Population(
        [
            _multi(1, {"tput": 100.0, "lat": 80.0}),  # parent
            _multi(2, {"tput": 80.0, "lat": 50.0}),  # frontier (only one besides parent)
            _multi(3, {"tput": 70.0, "lat": 90.0}),  # dominated
            _multi(4, {"tput": 60.0, "lat": 100.0}),  # dominated
        ]
    )
    rng = random.Random(0)  # noqa: S311  # tracked: #288
    picks = pop.select_inspirations(
        parent_id=1,
        k_top=3,
        k_random=0,
        rng=rng,
        space=_TPUT_LAT,
    )
    ids = [p.id for p in picks]
    assert ids[0] == 2  # frontier first
    # The next two slots come from non-frontier, sorted by perf_metric.
    # perf_metric == primary metric (set in _multi). For non-frontier pool
    # {3, 4}, primaries are 70 and 60 → id=3 before id=4.
    assert ids[1:] == [3, 4]


def test_select_inspiration_backfill_respects_min_primary() -> None:
    pop = Population(
        [
            _multi(1, {"lat": 10.0, "quality": 100.0}),
            _multi(2, {"lat": 12.0, "quality": 110.0}),
            _multi(3, {"lat": 20.0, "quality": 80.0}),
            _multi(4, {"lat": 30.0, "quality": 70.0}),
        ]
    )

    picks = pop.select_inspirations(
        parent_id=1,
        k_top=3,
        k_random=0,
        rng=random.Random(0),  # noqa: S311  # deterministic selection test
        space=_LAT_QUALITY,
    )

    assert [pick.id for pick in picks] == [2, 3, 4]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_individual_rejects_non_finite_perf_metric(value):  # noqa: ANN001, ANN201  # tracked: #288
    with pytest.raises(ValueError, match="perf_metric must be a finite number"):
        _passed(1, value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_individual_rejects_non_finite_multi_objective_metric(value):  # noqa: ANN001, ANN201  # tracked: #288
    with pytest.raises(ValueError, match=r"metrics\['throughput'\] must be a finite number"):
        Individual(
            id=1,
            generation=1,
            parent_id=None,
            commit="sha-1",
            perf_metric=10.0,
            metrics={"throughput": value},
            passed=True,
        )


def test_best_revalidates_mutated_fitness_before_ranking():  # noqa: ANN201  # tracked: #288
    individual = _passed(1, 10.0)
    population = Population([individual])
    individual.perf_metric = float("nan")

    with pytest.raises(ValueError, match="perf_metric must be a finite number"):
        population.best(MetricSpace())


def test_frontier_revalidates_mutated_metrics_before_comparison():  # noqa: ANN201  # tracked: #288
    individual = Individual(
        id=1,
        generation=1,
        parent_id=None,
        commit="sha-1",
        perf_metric=10.0,
        metrics={"throughput": 10.0},
        passed=True,
    )
    population = Population([individual])
    individual.metrics["throughput"] = float("inf")

    with pytest.raises(ValueError, match=r"metrics\['throughput'\] must be a finite number"):
        population.frontier(_space(Objective(name="throughput", direction="max")))


def test_softmax_revalidates_mutated_fitness_before_sampling():  # noqa: ANN201  # tracked: #288
    individual = _passed(1, 10.0)
    population = Population([individual])
    individual.perf_metric = float("-inf")

    with pytest.raises(ValueError, match="perf_metric must be a finite number"):
        population.select_parent(rng=random.Random(0), space=MetricSpace())  # noqa: S311  # tracked: #288


# ---------------------------------------------------------------------------
# Noise-aware selection
# ---------------------------------------------------------------------------


def _near_frontier() -> Population:
    """Two candidates 3% apart on both axes: a tie only under a tolerance."""
    return Population(
        [
            _multi(1, {"tput": 100.0, "lat": 80.0}),
            _multi(2, {"tput": 97.0, "lat": 82.4}),
        ]
    )


def _tput_lat(noise: float) -> MetricSpace:
    return _space(
        Objective(name="tput", direction="max"),
        Objective(name="lat", direction="min"),
        noise=noise,
    )


def test_frontier_retains_a_candidate_within_the_declared_tolerance():  # noqa: ANN201  # tracked: #288
    """Behavior change: selection now honors the task's measurement tolerance.

    Individual 2 is 3% behind on both axes. Compared exactly it is dominated;
    within a declared 5% tolerance that difference is not a measured result, so
    the candidate stays on the frontier and remains eligible as a parent.
    """
    assert {i.id for i in _near_frontier().frontier(_tput_lat(0.05))} == {1, 2}


def test_a_zero_tolerance_reproduces_the_previous_exact_frontier():  # noqa: ANN201  # tracked: #288
    """The pre-change behavior is the zero-tolerance case of the same rule."""
    assert {i.id for i in _near_frontier().frontier(_tput_lat(0.0))} == {1}


def test_a_candidate_beyond_the_tolerance_is_still_dominated():  # noqa: ANN201  # tracked: #288
    """The tolerance widens the tie band; it does not disable dominance."""
    assert {i.id for i in _near_frontier().frontier(_tput_lat(0.01))} == {1}


def test_best_prefers_the_latest_of_two_readings_within_the_tolerance():  # noqa: ANN201  # tracked: #288
    """Behavior change: a near-tie on the headline axis is a tie.

    Individual 2 measures 2% lower, which a 5% tolerance calls
    indistinguishable. ``Population.best`` breaks ties by latest id, which it
    expresses to ``MetricSpace.best`` by ordering newest first.
    """
    population = Population([_passed(1, 100.0), _passed(2, 98.0)])
    tolerant = _space(Objective(name="perf_metric", direction="max"), noise=0.05)

    best = population.best(tolerant)

    assert best is not None
    assert best.id == 2


def test_best_with_a_zero_tolerance_still_takes_the_strictly_higher_reading():  # noqa: ANN201  # tracked: #288
    """The pre-change behavior: only an exact tie goes to the latest id."""
    strict = Population([_passed(1, 100.0), _passed(2, 98.0)]).best(MetricSpace())
    tied = Population([_passed(1, 100.0), _passed(2, 100.0)]).best(MetricSpace())

    assert strict is not None
    assert strict.id == 1
    assert tied is not None
    assert tied.id == 2


def test_best_orders_on_the_primary_axis_when_the_task_declares_one():  # noqa: ANN201  # tracked: #288
    """A minimized headline axis makes the lowest reading the champion."""
    population = Population([_multi(1, {"latency_ms": 80.0}), _multi(2, {"latency_ms": 50.0})])

    best = population.best(_space(Objective(name="latency_ms", direction="min")))

    assert best is not None
    assert best.id == 2


def test_frontier_bias_can_still_reach_a_near_tie_parent():  # noqa: ANN201  # tracked: #288
    """The retained near-tie is a real selection outcome, not just a listing."""
    population = _near_frontier()
    rng = random.Random(0)  # noqa: S311  # tracked: #288

    picked = {
        _selected_id(population.select_parent(rng=rng, space=_tput_lat(0.05), frontier_bias=1.0))
        for _ in range(200)
    }

    assert picked == {1, 2}
