"""Contract tests for native and OpenEvolve-backed search policies."""

from __future__ import annotations

import json
import math
import random
import shutil
from importlib.metadata import version
from typing import TYPE_CHECKING, TypedDict

import pytest

from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.loops.evolve.population import Individual, Population
from vibesys.loops.evolve.search_policy import (
    OpenEvolveSearchConfig,
    OpenEvolveSearchPolicy,
    SearchSelectionParameters,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path


class _IterationOrderSet(set[str]):
    """Set whose iteration order can model a differently reconstructed process."""

    def __init__(self, values: Iterable[str], iteration_order: Iterable[str]) -> None:
        super().__init__(values)
        self._iteration_order = tuple(iteration_order)

    def __iter__(self) -> Iterator[str]:
        return iter(self._iteration_order)


class _UnusedRandom(random.Random):
    """Caller RNG placeholder: OpenEvolve selection uses its persisted stream."""

    def __init__(self) -> None:
        pass


class _PersistedAdapter(TypedDict):
    active_program_ids: list[str]


def _individual(
    individual_id: int,
    *,
    parent_id: int | None = None,
    generation: int = 0,
    perf: float | None = 10.0,
    metrics: dict[str, float] | None = None,
) -> Individual:
    return Individual(
        id=individual_id,
        generation=generation,
        parent_id=parent_id,
        commit=f"commit-{individual_id}",
        perf_metric=perf,
        perf_unit="ops/s",
        metrics=dict(metrics or {}),
        passed=True,
        summary=f"candidate {individual_id}",
    )


def _config(**overrides: object) -> OpenEvolveSearchConfig:
    values = {
        "population_size": 20,
        "archive_size": 10,
        "num_islands": 2,
        "migration_interval": 1,
        "migration_rate": 1.0,
    }
    values.update(overrides)
    return OpenEvolveSearchConfig(**values)


def _selection_parameters(
    *, k_top_inspirations: int = 0, k_random_inspirations: int = 0
) -> SearchSelectionParameters:
    return SearchSelectionParameters(
        rng=_UnusedRandom(),
        k_top_inspirations=k_top_inspirations,
        k_random_inspirations=k_random_inspirations,
        selection_temperature=0.5,
        space=MetricSpace(),
        frontier_bias=0.7,
    )


def _persisted_dir(state_dir: Path) -> Path:
    return state_dir / "snapshots" / (state_dir / "CURRENT").read_text()


def _persisted_adapter(state_dir: Path) -> _PersistedAdapter:
    return json.loads((_persisted_dir(state_dir) / "adapter.json").read_text())


def _persisted_program(state_dir: Path, program_id: str) -> dict[str, object]:
    return json.loads((_persisted_dir(state_dir) / "programs" / f"{program_id}.json").read_text())


def test_dependency_is_pinned_to_requested_release() -> None:
    assert version("openevolve") == "0.3.1"


def test_initialization_persists_empty_policy_for_bootstrap_resume(tmp_path: Path) -> None:
    config = _config()
    OpenEvolveSearchPolicy(state_dir=tmp_path, seed=7, config=config, space=MetricSpace())

    assert OpenEvolveSearchPolicy.has_state(tmp_path)
    assert OpenEvolveSearchPolicy.persisted_config(tmp_path) == config
    OpenEvolveSearchPolicy(state_dir=tmp_path, seed=999, config=None, space=MetricSpace())
    assert _persisted_adapter(tmp_path)["active_program_ids"] == []


def test_openevolve_selection_maps_programs_back_to_vibesys_individuals(tmp_path: Path) -> None:
    population = Population([_individual(1)])
    policy = OpenEvolveSearchPolicy(
        state_dir=tmp_path, seed=7, config=_config(), space=MetricSpace()
    )
    seed = population.passed[0]
    policy.record(
        seed,
        code="diff --git a/src/lib.rs b/src/lib.rs\n+seed",
        policy_parent_id=None,
        target_island=0,
        space=MetricSpace(),
    )

    selection = policy.select(
        population,
        _selection_parameters(k_top_inspirations=1, k_random_inspirations=1),
    )

    assert selection is not None
    assert selection.parent.id == 1
    assert selection.policy_parent_id == "vibesys-1"
    assert selection.target_island == 0
    persisted = _persisted_dir(tmp_path)
    assert (persisted / "metadata.json").is_file()
    assert (persisted / "programs" / "vibesys-1.json").is_file()


def test_migrants_keep_vibesys_identity_and_state_resumes(tmp_path: Path) -> None:
    seed = _individual(1)
    population = Population([seed])
    policy = OpenEvolveSearchPolicy(
        state_dir=tmp_path, seed=3, config=_config(), space=MetricSpace()
    )
    policy.record(
        seed,
        code="seed patch",
        policy_parent_id=None,
        target_island=0,
        space=MetricSpace(),
    )
    child = _individual(2, parent_id=1, generation=1, perf=11.0)
    population.add(child)
    policy.record(
        child,
        code="child patch",
        policy_parent_id="vibesys-1",
        target_island=0,
        space=MetricSpace(),
    )

    resumed = OpenEvolveSearchPolicy(
        state_dir=tmp_path, seed=3, config=_config(), space=MetricSpace()
    )
    selection = None
    for _ in range(2):
        selection = resumed.select(
            population,
            _selection_parameters(),
        )
        if selection is not None and selection.target_island == 1:
            break

    assert selection is not None
    assert selection.parent.id in {seed.id, child.id}
    assert selection.target_island == 1
    assert selection.policy_parent_id is not None
    selected_program = _persisted_program(tmp_path, selection.policy_parent_id)
    metadata = selected_program["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["vibesys_individual_id"] == selection.parent.id
    assert metadata["migrant"] is True


def test_resume_prunes_programs_evicted_before_save(tmp_path: Path) -> None:
    config = _config(population_size=2, archive_size=2, num_islands=1)
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=config, space=MetricSpace())
    for individual_id in range(1, 6):
        policy.record(
            _individual(individual_id),
            code=f"patch {individual_id}",
            policy_parent_id=None,
            target_island=0,
            space=MetricSpace(),
        )

    active_ids = set(_persisted_adapter(tmp_path)["active_program_ids"])
    OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=config, space=MetricSpace())

    assert len(active_ids) <= 2
    assert set(_persisted_adapter(tmp_path)["active_program_ids"]) == active_ids
    assert {
        path.stem for path in (_persisted_dir(tmp_path) / "programs").glob("*.json")
    } == active_ids


def test_replaying_population_does_not_readmit_evicted_individuals(tmp_path: Path) -> None:
    config = _config(
        population_size=2,
        archive_size=2,
        num_islands=1,
        migration_interval=50,
        migration_rate=0.0,
    )
    individuals = [
        _individual(individual_id, generation=individual_id - 1) for individual_id in range(1, 6)
    ]
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=config, space=MetricSpace())
    for individual in individuals:
        policy.record(
            individual,
            code=f"patch {individual.id}",
            policy_parent_id=None,
            target_island=0,
            space=MetricSpace(),
        )
    expected_programs = set(_persisted_adapter(tmp_path)["active_program_ids"])
    expected_generations = json.loads((_persisted_dir(tmp_path) / "metadata.json").read_text())[
        "island_generations"
    ]

    resumed = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=None, space=MetricSpace())
    for individual in individuals:
        resumed.record(
            individual,
            code=f"patch {individual.id}",
            policy_parent_id=None,
            target_island=0,
            space=MetricSpace(),
        )

    assert set(_persisted_adapter(tmp_path)["active_program_ids"]) == expected_programs
    assert (
        json.loads((_persisted_dir(tmp_path) / "metadata.json").read_text())["island_generations"]
        == expected_generations
    )


def test_resume_rejects_changed_database_topology(tmp_path: Path) -> None:
    policy = OpenEvolveSearchPolicy(
        state_dir=tmp_path, seed=4, config=_config(num_islands=2), space=MetricSpace()
    )
    policy.record(
        _individual(1),
        code="seed",
        policy_parent_id=None,
        target_island=0,
        space=MetricSpace(),
    )

    with pytest.raises(ValueError, match="does not match the resumed run"):
        OpenEvolveSearchPolicy(
            state_dir=tmp_path, seed=4, config=_config(num_islands=3), space=MetricSpace()
        )


def test_resume_rejects_changed_fitness_objective(tmp_path: Path) -> None:
    space = MetricSpace(objectives=(Objective(name="latency_ms", direction="min"),))
    policy = OpenEvolveSearchPolicy(
        state_dir=tmp_path,
        seed=4,
        config=_config(),
        space=space,
    )
    policy.record(
        _individual(1, metrics={"latency_ms": 4.0}),
        code="seed",
        policy_parent_id=None,
        target_island=0,
        space=space,
    )

    with pytest.raises(ValueError, match="fitness objective does not match"):
        OpenEvolveSearchPolicy(
            state_dir=tmp_path,
            seed=4,
            config=None,
            space=MetricSpace(objectives=(Objective(name="latency_ms", direction="max"),)),
        )


def test_finish_generation_keeps_iteration_monotonic(tmp_path: Path) -> None:
    policy = OpenEvolveSearchPolicy(
        state_dir=tmp_path, seed=4, config=_config(), space=MetricSpace()
    )
    policy.record(
        _individual(20),
        code="seed",
        policy_parent_id=None,
        target_island=0,
        space=MetricSpace(),
    )

    policy.finish_generation(1)

    assert (
        json.loads((_persisted_dir(tmp_path) / "metadata.json").read_text())["last_iteration"] == 20
    )


def test_zero_migration_rate_disables_upstream_minimum_migrant(tmp_path: Path) -> None:
    config = _config(migration_rate=0.0)
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=config, space=MetricSpace())
    policy.record(
        _individual(1),
        code="seed",
        policy_parent_id=None,
        target_island=0,
        space=MetricSpace(),
    )
    policy.record(
        _individual(2, parent_id=1, generation=1),
        code="child",
        policy_parent_id="vibesys-1",
        target_island=0,
        space=MetricSpace(),
    )

    assert not any(
        json.loads(path.read_text())["metadata"].get("migrant")
        for path in (_persisted_dir(tmp_path) / "programs").glob("*.json")
    )


def test_empty_island_copy_resolves_through_vibesys_ancestry(tmp_path: Path) -> None:
    config = _config(migration_interval=50, migration_rate=0.0)
    seed = _individual(1)
    population = Population([seed])
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=2, config=config, space=MetricSpace())
    policy.record(
        seed,
        code="seed",
        policy_parent_id=None,
        target_island=0,
        space=MetricSpace(),
    )

    selection = None
    for _ in range(2):
        selection = policy.select(
            population,
            _selection_parameters(),
        )
        if selection is not None and selection.target_island == 1:
            break

    assert selection is not None
    assert selection.parent.id == seed.id
    assert selection.target_island == 1
    assert selection.policy_parent_id is not None
    copy = _persisted_program(tmp_path, selection.policy_parent_id)
    metadata = copy["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["vibesys_individual_id"] == seed.id


def test_empty_island_copy_has_same_identity_after_resume(tmp_path: Path) -> None:
    config = _config(migration_interval=50, migration_rate=0.0)
    seed = _individual(1)
    population = Population([seed])
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=2, config=config, space=MetricSpace())
    policy.record(seed, code="seed", policy_parent_id=None, target_island=0, space=MetricSpace())
    resumed = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=999, config=None, space=MetricSpace())
    selection_parameters = _selection_parameters()

    island_selections = []
    for candidate in (policy, resumed):
        selection = None
        for _ in range(2):
            selection = candidate.select(population, selection_parameters)
            if selection is not None and selection.target_island == 1:
                break
        island_selections.append(selection)
    uninterrupted_selection, resumed_selection = island_selections
    assert uninterrupted_selection is not None
    assert resumed_selection is not None
    assert resumed_selection.policy_parent_id == uninterrupted_selection.policy_parent_id


def test_migration_has_same_program_identity_after_resume(tmp_path: Path) -> None:
    initial_dir = tmp_path / "initial"
    config = _config(num_islands=2, migration_interval=1, migration_rate=1.0)
    seed = _individual(1)
    initial = OpenEvolveSearchPolicy(
        state_dir=initial_dir, seed=3, config=config, space=MetricSpace()
    )
    initial.record(seed, code="seed", policy_parent_id=None, target_island=0, space=MetricSpace())
    uninterrupted_dir = tmp_path / "uninterrupted"
    resumed_dir = tmp_path / "resumed"
    shutil.copytree(initial_dir, uninterrupted_dir)
    shutil.copytree(initial_dir, resumed_dir)
    uninterrupted = OpenEvolveSearchPolicy(
        state_dir=uninterrupted_dir, seed=3, config=None, space=MetricSpace()
    )
    resumed = OpenEvolveSearchPolicy(
        state_dir=resumed_dir, seed=999, config=None, space=MetricSpace()
    )
    child = _individual(2, parent_id=1, generation=1)

    uninterrupted.record(
        child,
        code="child",
        policy_parent_id="vibesys-1",
        target_island=0,
        space=MetricSpace(),
    )
    resumed.record(
        child,
        code="child",
        policy_parent_id="vibesys-1",
        target_island=0,
        space=MetricSpace(),
    )

    uninterrupted_program_ids = set(_persisted_adapter(uninterrupted_dir)["active_program_ids"])
    resumed_program_ids = set(_persisted_adapter(resumed_dir)["active_program_ids"])
    assert uninterrupted_program_ids == resumed_program_ids
    assert {
        program_id: _persisted_program(uninterrupted_dir, program_id)["timestamp"]
        for program_id in uninterrupted_program_ids
    } == {
        program_id: _persisted_program(resumed_dir, program_id)["timestamp"]
        for program_id in resumed_program_ids
    }


def test_resume_continues_upstream_random_stream_without_touching_global_rng(
    tmp_path: Path,
) -> None:
    config = _config(num_islands=1, migration_rate=0.0)
    population = Population([_individual(individual_id) for individual_id in range(1, 6)])
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=19, config=config, space=MetricSpace())
    for individual in population.passed:
        policy.record(
            individual,
            code=f"patch {individual.id}",
            policy_parent_id=None,
            target_island=0,
            space=MetricSpace(),
        )

    global_state = random.getstate()
    selection_parameters = _selection_parameters()
    policy.select(population, selection_parameters)
    resumed = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=19, config=None, space=MetricSpace())
    program_ids = sorted(_persisted_adapter(tmp_path)["active_program_ids"])
    policy._database.islands[0] = _IterationOrderSet(  # noqa: SLF001  # LW-010048; injects unstable upstream set order because the public adapter exposes no ordering hook
        program_ids,
        program_ids,
    )
    resumed._database.islands[0] = _IterationOrderSet(  # noqa: SLF001  # LW-010036; simulates a resumed process reconstructing the same island in a different hash order
        program_ids,
        program_ids[1:] + program_ids[:1],
    )
    uninterrupted_next = policy.select(population, selection_parameters)
    resumed_next = resumed.select(population, selection_parameters)

    assert uninterrupted_next is not None
    assert resumed_next is not None
    assert resumed_next.policy_parent_id == uninterrupted_next.policy_parent_id
    assert random.getstate() == global_state


def test_selection_uses_lightweight_checkpoint_without_rewriting_programs(tmp_path: Path) -> None:
    config = _config(num_islands=1, migration_rate=0.0)
    seed = _individual(1)
    population = Population([seed])
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=2, config=config, space=MetricSpace())
    policy.record(
        seed,
        code="seed",
        policy_parent_id=None,
        target_island=0,
        space=MetricSpace(),
    )
    snapshot_before = (tmp_path / "CURRENT").read_text()

    selection = policy.select(
        population,
        _selection_parameters(),
    )

    assert selection is not None
    assert (tmp_path / "CURRENT").read_text() == snapshot_before
    assert (tmp_path / "selection.json").is_file()


def test_primary_min_objective_is_signed_for_openevolve_fitness(tmp_path: Path) -> None:
    space = MetricSpace(objectives=(Objective(name="latency_ms", direction="min"),))
    individual = _individual(1, perf=20.0, metrics={"latency_ms": 20.0})
    policy = OpenEvolveSearchPolicy(
        state_dir=tmp_path,
        seed=0,
        config=_config(num_islands=1),
        space=space,
    )
    policy.record(
        individual,
        code="latency patch",
        policy_parent_id=None,
        target_island=0,
        space=space,
    )

    program = _persisted_program(tmp_path, "vibesys-1")
    metrics = program["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["combined_score"] == -20.0


def _recorded_score(state_dir: Path, individual: Individual, space: MetricSpace) -> float:
    policy = OpenEvolveSearchPolicy(
        state_dir=state_dir,
        seed=0,
        config=_config(num_islands=1),
        space=space,
    )
    policy.record(
        individual,
        code="score fixture",
        policy_parent_id=None,
        target_island=0,
        space=space,
    )
    program = _persisted_program(state_dir, "vibesys-1")
    metrics = program["metrics"]
    assert isinstance(metrics, dict)
    return metrics["combined_score"]


@pytest.mark.parametrize(
    ("objective", "expected"),
    [
        (Objective(name="latency_ms", direction="max"), 20.0),
        (Objective(name="latency_ms", direction="min"), -20.0),
    ],
)
def test_primary_direction_signs_openevolve_perf_fallback(
    tmp_path: Path,
    objective: Objective,
    expected: float,
) -> None:
    space = MetricSpace(objectives=(objective,))
    individual = _individual(1, perf=20.0)

    assert _recorded_score(tmp_path, individual, space) == expected


def test_zero_perf_fallback_is_still_oriented_by_min_primary(tmp_path: Path) -> None:
    space = MetricSpace(objectives=(Objective(name="latency_ms", direction="min"),))
    individual = _individual(1, perf=0.0)

    score = _recorded_score(tmp_path, individual, space)
    assert math.copysign(1.0, score) == -1.0


def test_missing_perf_fallback_stays_neutral(tmp_path: Path) -> None:
    space = MetricSpace(objectives=(Objective(name="latency_ms", direction="min"),))

    assert _recorded_score(tmp_path, _individual(1, perf=None), space) == 0.0


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"population_size": 0}, "population_size must be >= 1"),
        ({"archive_size": 0}, "archive_size must be >= 1"),
        ({"num_islands": 0}, "num_islands must be >= 1"),
        ({"migration_interval": 0}, "migration_interval must be >= 1"),
        ({"migration_rate": -0.1}, r"migration_rate must be in \[0, 1\]"),
        ({"migration_rate": 1.1}, r"migration_rate must be in \[0, 1\]"),
    ],
)
def test_openevolve_config_rejects_invalid_values(
    kwargs: dict[str, int | float],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        _config(**kwargs)
