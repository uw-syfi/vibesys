"""An evolve run's public projection is its population, read from the strategy state."""

from __future__ import annotations

from vibesys.orchestration.evolve.core_policy.api import project_strategy_state
from vibesys.orchestration.evolve.population import PopulationConfig, PopulationSearch
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


def test_a_run_that_has_not_started_projects_no_population() -> None:
    projection = project_strategy_state(EvolveStrategyState(), experiment_revision=0)

    assert projection.payload is not None
    assert projection.payload["population"] is None
