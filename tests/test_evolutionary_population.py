"""Unit tests for vibeserve_agent.loops.evolve.population.

Pure-logic tests — no agent runner, no _RunContext, no GPU. The
``Population`` and ``Individual`` classes are intentionally free of
runtime imports so this file runs in isolation.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

import pytest

from vibeserve_agent.loops.evolve.population import (
    Individual,
    Objective,
    Population,
    _dominates,
)


# ---------------------------------------------------------------------------
# Individual: JSON round-trip
# ---------------------------------------------------------------------------


def test_individual_json_round_trip():
    ind = Individual(
        id=7,
        generation=3,
        parent_id=4,
        inspiration_ids=[1, 2, 3],
        commit="abc123",
        perf_metric=12.5,
        perf_unit="tok/s",
        passed=True,
        summary="swapped attention to FlashAttention",
        feedback="",
    )
    blob = ind.to_json()
    restored = Individual.from_json(blob)
    assert restored == ind


def test_individual_from_json_tolerates_missing_optional_fields():
    """Older population.json files might omit defaults — load anyway."""
    ind = Individual.from_json({"id": 1, "generation": 1, "parent_id": None})
    assert ind.id == 1
    assert ind.inspiration_ids == []
    assert ind.passed is False
    assert ind.summary == ""


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


def test_next_id_starts_at_one_and_increments():
    pop = Population()
    assert pop.next_id() == 1
    pop.add(_passed(1, 10.0))
    assert pop.next_id() == 2


def test_passed_filter_excludes_no_commit_and_failed():
    pop = Population([_passed(1, 10.0), _failed(2), _passed(3, 11.0)])
    # Add a synthetic "passed but no commit" — shouldn't be selectable.
    pop.add(Individual(id=4, generation=1, parent_id=None, passed=True, commit=None))
    ids = sorted(i.id for i in pop.passed)
    assert ids == [1, 3]


def test_best_picks_highest_perf_metric():
    pop = Population([_passed(1, 10.0), _passed(2, 12.0), _passed(3, 11.0)])
    assert pop.best().id == 2


def test_best_returns_none_with_no_passed_individuals():
    pop = Population([_failed(1), _failed(2)])
    assert pop.best() is None


def test_best_breaks_ties_by_id():
    pop = Population([_passed(1, 10.0), _passed(2, 10.0)])
    assert pop.best().id == 2


# ---------------------------------------------------------------------------
# Population.select_parent
# ---------------------------------------------------------------------------


def test_select_parent_empty_returns_none():
    assert Population().select_parent(rng=random.Random(0)) is None


def test_select_parent_only_failed_returns_none():
    pop = Population([_failed(1), _failed(2)])
    assert pop.select_parent(rng=random.Random(0)) is None


def test_select_parent_single_passed_returns_it():
    pop = Population([_failed(1), _passed(2, 10.0)])
    assert pop.select_parent(rng=random.Random(0)).id == 2


def test_select_parent_low_temperature_concentrates_on_best():
    """A near-zero temperature should pick the best almost every time."""
    pop = Population([_passed(1, 1.0), _passed(2, 5.0), _passed(3, 10.0)])
    rng = random.Random(123)
    counts = Counter(
        pop.select_parent(rng=rng, temperature=0.01).id for _ in range(200)
    )
    # The best (id=3) should dominate.
    assert counts[3] > 180


def test_select_parent_high_temperature_spreads():
    """High temperature flattens the distribution toward uniform."""
    pop = Population([_passed(1, 1.0), _passed(2, 5.0), _passed(3, 10.0)])
    rng = random.Random(123)
    counts = Counter(
        pop.select_parent(rng=rng, temperature=100.0).id for _ in range(600)
    )
    # All three should be picked a meaningful number of times.
    assert all(counts[i] > 100 for i in (1, 2, 3))


def test_select_parent_uniform_when_all_perfs_equal():
    pop = Population([_passed(1, 7.0), _passed(2, 7.0), _passed(3, 7.0)])
    rng = random.Random(42)
    counts = Counter(pop.select_parent(rng=rng).id for _ in range(300))
    assert all(counts[i] > 50 for i in (1, 2, 3))


# ---------------------------------------------------------------------------
# Population.select_inspirations
# ---------------------------------------------------------------------------


def test_select_inspirations_excludes_parent_and_dedupes():
    pop = Population(
        [_passed(i, float(i)) for i in range(1, 8)]  # ids 1..7, perf 1..7
    )
    rng = random.Random(0)
    picks = pop.select_inspirations(parent_id=7, k_top=2, k_random=2, rng=rng)
    ids = [i.id for i in picks]
    assert 7 not in ids  # parent excluded
    assert len(set(ids)) == len(ids)  # no dupes


def test_select_inspirations_top_first_then_random():
    pop = Population([_passed(i, float(i)) for i in range(1, 8)])
    rng = random.Random(0)
    picks = pop.select_inspirations(parent_id=1, k_top=2, k_random=2, rng=rng)
    # First two should be the top-2 highest-perf (ids 7 and 6).
    assert picks[0].id == 7
    assert picks[1].id == 6
    # The remaining slots are random over {2, 3, 4, 5}.
    rest = {p.id for p in picks[2:]}
    assert rest.issubset({2, 3, 4, 5})


def test_select_inspirations_handles_small_population():
    pop = Population([_passed(1, 5.0), _passed(2, 6.0)])
    picks = pop.select_inspirations(
        parent_id=1, k_top=2, k_random=2, rng=random.Random(0),
    )
    # Only one other passed individual exists; no dupes / no errors.
    assert [p.id for p in picks] == [2]


def test_select_inspirations_empty_when_only_parent_passed():
    pop = Population([_passed(1, 5.0), _failed(2)])
    picks = pop.select_inspirations(
        parent_id=1, k_top=3, k_random=3, rng=random.Random(0),
    )
    assert picks == []


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_population_save_and_load_round_trip(tmp_path):
    src = Population([_passed(1, 10.0), _failed(2), _passed(3, 11.5)])
    path = tmp_path / "subdir" / "population.json"  # exercises mkdir
    src.save(path)
    assert path.exists()

    loaded = Population.load(path)
    assert [i.id for i in loaded.all] == [1, 2, 3]
    assert loaded.best().id == 3
    # Parent id shape preserved (None survives JSON round-trip).
    assert loaded.get(1).parent_id is None


def test_population_load_missing_returns_empty(tmp_path):
    pop = Population.load(tmp_path / "does-not-exist.json")
    assert len(pop) == 0
    assert pop.best() is None


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


def test_objective_rejects_unknown_direction():
    with pytest.raises(ValueError):
        Objective(name="foo", direction="bigger")


def test_objective_signed_max_passes_through():
    assert Objective("x", "max").signed(5.0) == 5.0


def test_objective_signed_min_negates():
    assert Objective("x", "min").signed(5.0) == -5.0


def test_dominates_max_objective():
    a = _multi(1, {"throughput": 100.0})
    b = _multi(2, {"throughput": 80.0})
    objs = [Objective("throughput", "max")]
    assert _dominates(a, b, objs) is True
    assert _dominates(b, a, objs) is False


def test_dominates_min_objective():
    a = _multi(1, {"latency_ms": 50.0})
    b = _multi(2, {"latency_ms": 80.0})
    objs = [Objective("latency_ms", "min")]
    assert _dominates(a, b, objs) is True


def test_dominates_requires_strictly_better_on_at_least_one():
    """Equal on every axis → no domination either way."""
    a = _multi(1, {"x": 5.0, "y": 5.0})
    b = _multi(2, {"x": 5.0, "y": 5.0})
    objs = [Objective("x", "max"), Objective("y", "max")]
    assert _dominates(a, b, objs) is False
    assert _dominates(b, a, objs) is False


def test_dominates_two_axis_mixed_is_non_dominated():
    """Throughput/latency tradeoff: neither dominates."""
    a = _multi(1, {"tput": 100.0, "lat": 80.0})
    b = _multi(2, {"tput": 80.0, "lat": 50.0})
    objs = [Objective("tput", "max"), Objective("lat", "min")]
    assert _dominates(a, b, objs) is False
    assert _dominates(b, a, objs) is False


def test_dominates_missing_metric_treats_as_incomparable():
    a = _multi(1, {"tput": 100.0})  # missing 'lat'
    b = _multi(2, {"tput": 80.0, "lat": 50.0})
    objs = [Objective("tput", "max"), Objective("lat", "min")]
    assert _dominates(a, b, objs) is False
    assert _dominates(b, a, objs) is False


def test_frontier_returns_only_non_dominated():
    pop = Population([
        _multi(1, {"tput": 100.0, "lat": 80.0}),  # frontier (high tput)
        _multi(2, {"tput": 80.0, "lat": 50.0}),   # frontier (low lat)
        _multi(3, {"tput": 70.0, "lat": 90.0}),   # dominated by id=1 and id=2
        _multi(4, {"tput": 90.0, "lat": 60.0}),   # frontier (middle)
    ])
    objs = [Objective("tput", "max"), Objective("lat", "min")]
    front_ids = {i.id for i in pop.frontier(objs)}
    assert front_ids == {1, 2, 4}


def test_frontier_excludes_individuals_missing_metrics():
    pop = Population([
        _multi(1, {"tput": 100.0, "lat": 80.0}),
        _multi(2, {"tput": 80.0}),  # missing 'lat'
    ])
    objs = [Objective("tput", "max"), Objective("lat", "min")]
    front_ids = {i.id for i in pop.frontier(objs)}
    # Only id=1 is fully metric'd.
    assert front_ids == {1}


def test_frontier_empty_when_no_objectives():
    pop = Population([_multi(1, {"tput": 100.0})])
    assert pop.frontier([]) == []


# ---------------------------------------------------------------------------
# Frontier-biased select_parent / select_inspirations
# ---------------------------------------------------------------------------


def test_select_parent_pareto_mode_draws_from_frontier_with_full_bias():
    """frontier_bias=1.0 → parent always sampled from the Pareto front."""
    pop = Population([
        _multi(1, {"tput": 100.0, "lat": 80.0}),  # frontier
        _multi(2, {"tput": 80.0, "lat": 50.0}),   # frontier
        _multi(3, {"tput": 50.0, "lat": 200.0}),  # dominated
    ])
    objs = [Objective("tput", "max"), Objective("lat", "min")]
    rng = random.Random(0)
    counts = Counter(
        pop.select_parent(rng=rng, objectives=objs, frontier_bias=1.0).id
        for _ in range(200)
    )
    assert counts[3] == 0  # never the dominated one


def test_select_parent_pareto_mode_falls_back_to_scalar_when_bias_zero():
    """frontier_bias=0.0 → bypasses the frontier branch, scalar softmax used.

    With temperature near 0, the highest perf_metric (id=1, perf=100) wins.
    """
    pop = Population([
        _multi(1, {"tput": 100.0, "lat": 80.0}),
        _multi(2, {"tput": 80.0, "lat": 50.0}),
    ])
    objs = [Objective("tput", "max"), Objective("lat", "min")]
    rng = random.Random(0)
    counts = Counter(
        pop.select_parent(
            rng=rng, objectives=objs, frontier_bias=0.0, temperature=0.01,
        ).id for _ in range(100)
    )
    assert counts[1] > 90


def test_select_parent_falls_back_when_frontier_is_empty():
    """No individual reports both objectives → frontier is empty →
    even with bias=1.0, scalar softmax kicks in so the loop isn't blocked."""
    pop = Population([
        _multi(1, {"tput": 100.0}),  # missing 'lat'
        _multi(2, {"tput": 80.0}),   # missing 'lat'
    ])
    objs = [Objective("tput", "max"), Objective("lat", "min")]
    rng = random.Random(0)
    pick = pop.select_parent(rng=rng, objectives=objs, frontier_bias=1.0)
    # We get *some* individual via scalar fallback rather than None.
    assert pick is not None
    assert pick.id in (1, 2)


def test_select_inspirations_pareto_mode_pulls_from_frontier_first():
    """Top slots come from the Pareto frontier sorted by primary objective."""
    pop = Population([
        _multi(1, {"tput": 100.0, "lat": 80.0}),  # frontier
        _multi(2, {"tput": 80.0, "lat": 50.0}),   # frontier
        _multi(3, {"tput": 90.0, "lat": 60.0}),   # frontier
        _multi(4, {"tput": 60.0, "lat": 100.0}),  # dominated
        _multi(5, {"tput": 50.0, "lat": 110.0}),  # dominated
    ])
    objs = [Objective("tput", "max"), Objective("lat", "min")]
    rng = random.Random(0)
    picks = pop.select_inspirations(
        parent_id=1,
        k_top=2,
        k_random=1,
        rng=rng,
        objectives=objs,
    )
    # First two slots are frontier members (excluding parent #1), sorted by
    # primary objective 'tput' descending: id=3 (tput=90) before id=2 (tput=80).
    assert picks[0].id == 3
    assert picks[1].id == 2
    # Random slot is filled from {4, 5} (non-frontier, non-parent, non-top).
    assert picks[2].id in (4, 5)


def test_select_inspirations_backfills_when_frontier_smaller_than_k_top():
    """When the frontier has only one non-parent member but k_top=3,
    fill the remaining slots from non-frontier passers."""
    pop = Population([
        _multi(1, {"tput": 100.0, "lat": 80.0}),  # parent
        _multi(2, {"tput": 80.0, "lat": 50.0}),   # frontier (only one besides parent)
        _multi(3, {"tput": 70.0, "lat": 90.0}),   # dominated
        _multi(4, {"tput": 60.0, "lat": 100.0}),  # dominated
    ])
    objs = [Objective("tput", "max"), Objective("lat", "min")]
    rng = random.Random(0)
    picks = pop.select_inspirations(
        parent_id=1,
        k_top=3,
        k_random=0,
        rng=rng,
        objectives=objs,
    )
    ids = [p.id for p in picks]
    assert ids[0] == 2  # frontier first
    # The next two slots come from non-frontier, sorted by perf_metric.
    # perf_metric == primary metric (set in _multi). For non-frontier pool
    # {3, 4}, primaries are 70 and 60 → id=3 before id=4.
    assert ids[1:] == [3, 4]


def test_population_save_writes_valid_json(tmp_path):
    """Sanity: the persisted file is a JSON list of records."""
    src = Population([_passed(1, 10.0)])
    path = tmp_path / "population.json"
    src.save(path)
    data = json.loads(path.read_text())
    assert isinstance(data, list)
    assert data[0]["id"] == 1
    assert data[0]["perf_metric"] == 10.0
