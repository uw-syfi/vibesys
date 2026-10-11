"""Read model of an evolve run: the population in the shape frontends consume."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.run.contracts import (
    IterationKind,
    IterationMetric,
    IterationStatus,
    IterationSummary,
    IterationTree,
    PluginProjection,
)

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
        iterations=_iterations(state),
    )


def _iterations(state: EvolveStrategyState) -> IterationTree:
    """Map generations and candidates without borrowing round semantics."""
    population = state.population
    if population is None:
        return IterationTree.from_nodes(
            (
                IterationSummary(
                    id="generation:0",
                    kind=IterationKind.GENERATION,
                    label="Generation 0",
                    ordinal=0,
                    status=IterationStatus.PENDING,
                ),
            )
        )
    by_generation = {
        generation: tuple(
            sorted(
                (item for item in population.individuals if item.generation == generation),
                key=lambda item: item.id,
            )
        )
        for generation in range(population.generation + 1)
    }
    nodes: list[IterationSummary] = []
    for generation, candidates in by_generation.items():
        generation_id = f"generation:{generation}"
        nodes.append(
            IterationSummary(
                id=generation_id,
                kind=IterationKind.GENERATION,
                label=f"Generation {generation}",
                ordinal=generation,
                status=_generation_status(
                    state,
                    generation,
                    current_generation=population.generation,
                    bootstrap_has_candidates=bool(candidates),
                ),
                child_count=len(candidates),
            )
        )
        for ordinal, candidate in enumerate(candidates):
            metrics = tuple(
                IterationMetric(
                    id=name,
                    value=value,
                    unit=candidate.perf_unit if index == 0 else None,
                )
                for index, (name, value) in enumerate(candidate.metrics[:4])
            )
            if not metrics and candidate.perf_metric is not None:
                metrics = (
                    IterationMetric(
                        id="performance",
                        value=candidate.perf_metric,
                        unit=candidate.perf_unit,
                    ),
                )
            nodes.append(
                IterationSummary(
                    id=f"candidate:{candidate.id}",
                    parent_id=generation_id,
                    kind=IterationKind.CANDIDATE,
                    label=candidate.summary or f"Candidate {candidate.id}",
                    ordinal=ordinal,
                    status=(
                        IterationStatus.COMPLETED if candidate.passed else IterationStatus.FAILED
                    ),
                    headline_metrics=metrics,
                )
            )
    return IterationTree.from_nodes(tuple(nodes))


def _generation_status(
    state: EvolveStrategyState,
    generation: int,
    *,
    current_generation: int,
    bootstrap_has_candidates: bool,
) -> IterationStatus:
    if generation < current_generation:
        return IterationStatus.COMPLETED
    if generation == current_generation and state.generation_start is not None:
        return IterationStatus.ACTIVE
    if generation > 0:
        return IterationStatus.COMPLETED
    return IterationStatus.COMPLETED if bootstrap_has_candidates else IterationStatus.PENDING
