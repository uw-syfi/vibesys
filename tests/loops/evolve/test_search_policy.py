"""Contract tests for native and OpenEvolve-backed search policies."""

from __future__ import annotations

import json
import random
from collections.abc import Iterable, Iterator
from importlib.metadata import version
from pathlib import Path

import pytest

from vibesys.loops.evolve.population import Individual, Objective, Population
from vibesys.loops.evolve.search_policy import (
    OpenEvolveSearchConfig,
    OpenEvolveSearchPolicy,
)


class _IterationOrderSet(set[str]):
    """Set whose iteration order can model a differently reconstructed process."""

    def __init__(self, values: Iterable[str], iteration_order: Iterable[str]) -> None:
        super().__init__(values)
        self._iteration_order = tuple(iteration_order)

    def __iter__(self) -> Iterator[str]:
        return iter(self._iteration_order)


def _individual(
    individual_id: int,
    *,
    parent_id: int | None = None,
    generation: int = 0,
    perf: float = 10.0,
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
    return OpenEvolveSearchConfig(**values)  # type: ignore[arg-type]


def _persisted_dir(state_dir: Path) -> Path:
    return state_dir / "snapshots" / (state_dir / "CURRENT").read_text()


def test_dependency_is_pinned_to_requested_release() -> None:
    assert version("openevolve") == "0.3.1"


def test_initialization_persists_empty_policy_for_bootstrap_resume(tmp_path) -> None:
    config = _config()
    OpenEvolveSearchPolicy(state_dir=tmp_path, seed=7, config=config)

    assert OpenEvolveSearchPolicy.has_state(tmp_path)
    assert OpenEvolveSearchPolicy.persisted_config(tmp_path) == config
    resumed = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=999, config=None)
    assert resumed._database.programs == {}


def test_openevolve_selection_maps_programs_back_to_vibesys_individuals(tmp_path) -> None:
    population = Population([_individual(1)])
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=7, config=_config())
    seed = population.passed[0]
    policy.record(
        seed,
        code="diff --git a/src/lib.rs b/src/lib.rs\n+seed",
        policy_parent_id=None,
        target_island=0,
        objectives=None,
    )

    selection = policy.select(
        population,
        rng=random.Random(99),
        k_top_inspirations=1,
        k_random_inspirations=1,
        selection_temperature=0.5,
        objectives=None,
        frontier_bias=0.7,
    )

    assert selection is not None
    assert selection.parent.id == 1
    assert selection.policy_parent_id == "vibesys-1"
    assert selection.target_island == 0
    persisted = _persisted_dir(tmp_path)
    assert (persisted / "metadata.json").is_file()
    assert (persisted / "programs" / "vibesys-1.json").is_file()


def test_migrants_keep_vibesys_identity_and_state_resumes(tmp_path) -> None:
    seed = _individual(1)
    population = Population([seed])
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=3, config=_config())
    policy.record(
        seed,
        code="seed patch",
        policy_parent_id=None,
        target_island=0,
        objectives=None,
    )
    child = _individual(2, parent_id=1, generation=1, perf=11.0)
    population.add(child)
    policy.record(
        child,
        code="child patch",
        policy_parent_id="vibesys-1",
        target_island=0,
        objectives=None,
    )

    resumed = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=3, config=_config())
    resumed._database.set_current_island(1)
    selection = resumed.select(
        population,
        rng=random.Random(1),
        k_top_inspirations=0,
        k_random_inspirations=0,
        selection_temperature=0.5,
        objectives=None,
        frontier_bias=0.7,
    )

    assert selection is not None
    assert selection.parent.id in {seed.id, child.id}
    assert selection.target_island == 1
    selected_program = resumed._database.programs[selection.policy_parent_id]
    assert selected_program.metadata["vibesys_individual_id"] == selection.parent.id
    assert selected_program.metadata["migrant"] is True


def test_resume_prunes_programs_evicted_before_save(tmp_path) -> None:
    config = _config(population_size=2, archive_size=2, num_islands=1)
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=config)
    for individual_id in range(1, 6):
        policy.record(
            _individual(individual_id),
            code=f"patch {individual_id}",
            policy_parent_id=None,
            target_island=0,
            objectives=None,
        )

    active_ids = set(policy._database.programs)
    resumed = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=config)

    assert len(active_ids) <= 2
    assert set(resumed._database.programs) == active_ids
    assert {
        path.stem for path in (_persisted_dir(tmp_path) / "programs").glob("*.json")
    } == active_ids


def test_replaying_population_does_not_readmit_evicted_individuals(tmp_path) -> None:
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
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=config)
    for individual in individuals:
        policy.record(
            individual,
            code=f"patch {individual.id}",
            policy_parent_id=None,
            target_island=0,
            objectives=None,
        )
    expected_programs = set(policy._database.programs)
    expected_generations = list(policy._database.island_generations)

    resumed = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=None)
    for individual in individuals:
        resumed.record(
            individual,
            code=f"patch {individual.id}",
            policy_parent_id=None,
            target_island=0,
            objectives=None,
        )

    assert set(resumed._database.programs) == expected_programs
    assert resumed._database.island_generations == expected_generations


def test_resume_rejects_changed_database_topology(tmp_path) -> None:
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=_config(num_islands=2))
    policy.record(
        _individual(1),
        code="seed",
        policy_parent_id=None,
        target_island=0,
        objectives=None,
    )

    with pytest.raises(ValueError, match="does not match the resumed run"):
        OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=_config(num_islands=3))


def test_resume_rejects_changed_fitness_objective(tmp_path) -> None:
    objectives = [Objective("latency_ms", "min")]
    policy = OpenEvolveSearchPolicy(
        state_dir=tmp_path,
        seed=4,
        config=_config(),
        objectives=objectives,
    )
    policy.record(
        _individual(1, metrics={"latency_ms": 4.0}),
        code="seed",
        policy_parent_id=None,
        target_island=0,
        objectives=objectives,
    )

    with pytest.raises(ValueError, match="fitness objective does not match"):
        OpenEvolveSearchPolicy(
            state_dir=tmp_path,
            seed=4,
            config=None,
            objectives=[Objective("latency_ms", "max")],
        )


def test_finish_generation_keeps_iteration_monotonic(tmp_path) -> None:
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=_config())
    policy.record(
        _individual(20),
        code="seed",
        policy_parent_id=None,
        target_island=0,
        objectives=None,
    )

    policy.finish_generation(1)

    assert policy._database.last_iteration == 20
    assert (
        json.loads((_persisted_dir(tmp_path) / "metadata.json").read_text())["last_iteration"] == 20
    )


def test_zero_migration_rate_disables_upstream_minimum_migrant(tmp_path) -> None:
    config = _config(migration_rate=0.0)
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=4, config=config)
    policy.record(
        _individual(1),
        code="seed",
        policy_parent_id=None,
        target_island=0,
        objectives=None,
    )
    policy.record(
        _individual(2, parent_id=1, generation=1),
        code="child",
        policy_parent_id="vibesys-1",
        target_island=0,
        objectives=None,
    )

    assert not any(
        program.metadata.get("migrant") for program in policy._database.programs.values()
    )


def test_empty_island_copy_resolves_through_vibesys_ancestry(tmp_path) -> None:
    config = _config(migration_interval=50, migration_rate=0.0)
    seed = _individual(1)
    population = Population([seed])
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=2, config=config)
    policy.record(
        seed,
        code="seed",
        policy_parent_id=None,
        target_island=0,
        objectives=None,
    )
    policy._database.set_current_island(1)
    policy._database.config.exploration_ratio = 1.0
    policy._database.config.exploitation_ratio = 0.0

    selection = policy.select(
        population,
        rng=random.Random(1),
        k_top_inspirations=0,
        k_random_inspirations=0,
        selection_temperature=0.5,
        objectives=None,
        frontier_bias=0.7,
    )

    assert selection is not None
    assert selection.parent.id == seed.id
    assert selection.target_island == 1
    copy = policy._database.programs[selection.policy_parent_id]
    assert copy.metadata["vibesys_individual_id"] == seed.id


def test_resume_continues_upstream_random_stream_without_touching_global_rng(tmp_path) -> None:
    config = _config(num_islands=1, migration_rate=0.0)
    population = Population([_individual(individual_id) for individual_id in range(1, 6)])
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=19, config=config)
    for individual in population.passed:
        policy.record(
            individual,
            code=f"patch {individual.id}",
            policy_parent_id=None,
            target_island=0,
            objectives=None,
        )

    global_state = random.getstate()
    selection_args = {
        "rng": random.Random(1),
        "k_top_inspirations": 0,
        "k_random_inspirations": 0,
        "selection_temperature": 0.5,
        "objectives": None,
        "frontier_bias": 0.7,
    }
    policy.select(population, **selection_args)
    resumed = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=19, config=None)
    program_ids = sorted(policy._database.programs)
    policy._database.config.exploration_ratio = 1.0
    resumed._database.config.exploration_ratio = 1.0
    policy._database.islands[0] = _IterationOrderSet(program_ids, program_ids)
    resumed._database.islands[0] = _IterationOrderSet(
        program_ids,
        program_ids[1:] + program_ids[:1],
    )
    uninterrupted_next = policy.select(population, **selection_args)
    resumed_next = resumed.select(population, **selection_args)

    assert uninterrupted_next is not None and resumed_next is not None
    assert resumed_next.policy_parent_id == uninterrupted_next.policy_parent_id
    assert random.getstate() == global_state


def test_selection_uses_lightweight_checkpoint_without_rewriting_programs(tmp_path) -> None:
    config = _config(num_islands=1, migration_rate=0.0)
    seed = _individual(1)
    population = Population([seed])
    policy = OpenEvolveSearchPolicy(state_dir=tmp_path, seed=2, config=config)
    policy.record(
        seed,
        code="seed",
        policy_parent_id=None,
        target_island=0,
        objectives=None,
    )
    snapshot_before = (tmp_path / "CURRENT").read_text()

    selection = policy.select(
        population,
        rng=random.Random(1),
        k_top_inspirations=0,
        k_random_inspirations=0,
        selection_temperature=0.5,
        objectives=None,
        frontier_bias=0.7,
    )

    assert selection is not None
    assert (tmp_path / "CURRENT").read_text() == snapshot_before
    assert (tmp_path / "selection.json").is_file()


def test_primary_min_objective_is_signed_for_openevolve_fitness(tmp_path) -> None:
    objectives = [Objective("latency_ms", "min")]
    individual = _individual(1, perf=20.0, metrics={"latency_ms": 20.0})
    policy = OpenEvolveSearchPolicy(
        state_dir=tmp_path,
        seed=0,
        config=_config(num_islands=1),
        objectives=objectives,
    )
    policy.record(
        individual,
        code="latency patch",
        policy_parent_id=None,
        target_island=0,
        objectives=objectives,
    )

    assert policy._database.programs["vibesys-1"].metrics["combined_score"] == -20.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"population_size": 0},
        {"archive_size": 0},
        {"num_islands": 0},
        {"migration_interval": 0},
        {"migration_rate": -0.1},
        {"migration_rate": 1.1},
    ],
)
def test_openevolve_config_rejects_invalid_values(kwargs) -> None:
    with pytest.raises(ValueError):
        _config(**kwargs)
