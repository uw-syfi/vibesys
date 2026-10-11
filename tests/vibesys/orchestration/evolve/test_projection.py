"""An evolve run's public projection is its population, read from the strategy state."""

from __future__ import annotations

from vibesys.api import IterationStatus, IterationTree
from vibesys.orchestration.evolve.core_policy.api import project_strategy_state
from vibesys.orchestration.evolve.population import (
    CandidateOutcome,
    PopulationConfig,
    PopulationSearch,
)
from vibesys.orchestration.evolve.strategy.api import EvolveStrategyState


def test_the_projection_carries_the_population() -> None:
    population = PopulationSearch(PopulationConfig(seed=3)).initial()
    state = EvolveStrategyState(population=population)

    projection = project_strategy_state(state, experiment_revision=4)

    assert projection.experiment_revision == 4
    assert projection.payload == {
        "population": population.model_dump(mode="json"),
        "generation": 0,
        "stage": "bootstrap",
    }
    assert isinstance(projection.iterations, IterationTree)
    assert projection.iterations.nodes[0].status is IterationStatus.PENDING


def test_a_run_that_has_not_started_projects_no_population() -> None:
    projection = project_strategy_state(EvolveStrategyState(), experiment_revision=0)

    assert projection.payload is not None
    assert projection.payload["population"] is None
    assert isinstance(projection.iterations, IterationTree)
    assert projection.iterations.nodes[0].status is IterationStatus.PENDING


def test_generation_status_follows_durable_population_lifecycle() -> None:
    search = PopulationSearch(PopulationConfig(seed=3))
    _, seeded = search.admit(
        search.initial(),
        CandidateOutcome(passed=True, parent_id=None, summary="seed", commit="seed"),
    )
    completed = project_strategy_state(
        EvolveStrategyState(population=seeded), experiment_revision=1
    )
    assert isinstance(completed.iterations, IterationTree)
    assert completed.iterations.nodes[0].status is IterationStatus.COMPLETED

    started = search.end_generation(seeded)
    active = project_strategy_state(
        EvolveStrategyState(population=started, generation_start=seeded), experiment_revision=2
    )
    assert isinstance(active.iterations, IterationTree)
    generation = next(node for node in active.iterations.nodes if node.id == "generation:1")
    assert generation.status is IterationStatus.ACTIVE


def test_all_skipped_generation_is_completed() -> None:
    search = PopulationSearch(PopulationConfig(seed=3))
    _, seeded = search.admit(
        search.initial(),
        CandidateOutcome(passed=True, parent_id=None, summary="seed", commit="seed"),
    )
    generation = search.end_generation(seeded)

    projection = project_strategy_state(
        EvolveStrategyState(population=generation), experiment_revision=2
    )

    assert isinstance(projection.iterations, IterationTree)
    completed = next(node for node in projection.iterations.nodes if node.id == "generation:1")
    assert completed.child_count == 0
    assert completed.status is IterationStatus.COMPLETED
