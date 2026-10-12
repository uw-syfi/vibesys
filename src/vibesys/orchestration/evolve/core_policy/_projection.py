"""Read model of an evolve run: the population in the shape frontends consume."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.run.contracts import PluginProjection

if TYPE_CHECKING:
    from vibesys.orchestration.evolve.strategy.api import EvolveStrategyState


def project_strategy_state(
    state: EvolveStrategyState, *, experiment_revision: int
) -> PluginProjection:
    """Project one strategy state; `experiment_revision` is the core revision it was read at."""
    population = state.population
    return PluginProjection(
        payload={
            "population": None if population is None else population.model_dump(mode="json"),
            "generation": 0 if population is None else population.generation,
            "stage": state.stage.value,
        },
        experiment_revision=experiment_revision,
    )
