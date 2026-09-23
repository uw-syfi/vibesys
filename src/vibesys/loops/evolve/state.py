"""Typed persistence adapter for evolutionary population state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.loops.evolve.population import Individual, Population
from vibesys.loops.metrics import MetricSpace
from vs_loop_state.api import IndividualRecord, PopulationSnapshot

if TYPE_CHECKING:
    from vs_project.api import StateNamespace, StateSlot

_POPULATION_FILE = "population.json"
_METRICS_FILE = "metrics.json"


def population_snapshot(population: Population) -> PopulationSnapshot:
    """Convert an in-memory population into its strict persisted contract."""
    return PopulationSnapshot(
        individuals=tuple(
            IndividualRecord(
                id=individual.id,
                generation=individual.generation,
                parent_id=individual.parent_id,
                inspiration_ids=tuple(individual.inspiration_ids),
                commit=individual.commit,
                perf_metric=individual.perf_metric,
                perf_unit=individual.perf_unit,
                metrics=dict(individual.metrics),
                passed=individual.passed,
                summary=individual.summary,
                feedback=individual.feedback,
                policy_parent_id=individual.policy_parent_id,
                policy_target_island=individual.policy_target_island,
            )
            for individual in population.all
        )
    )


def population_from_snapshot(snapshot: PopulationSnapshot) -> Population:
    """Build an in-memory population from validated persisted records."""
    return Population(
        [
            Individual(
                id=record.id,
                generation=record.generation,
                parent_id=record.parent_id,
                inspiration_ids=list(record.inspiration_ids),
                commit=record.commit,
                perf_metric=record.perf_metric,
                perf_unit=record.perf_unit,
                metrics=dict(record.metrics),
                passed=record.passed,
                summary=record.summary,
                feedback=record.feedback,
                policy_parent_id=record.policy_parent_id,
                policy_target_island=record.policy_target_island,
            )
            for record in snapshot.individuals
        ]
    )


class EvolutionStateStore:
    """Persist a validated population inside one portable namespace."""

    def __init__(self, namespace: StateNamespace) -> None:
        """Bind the adapter to one portable evolve-loop namespace."""
        self._namespace = namespace
        self._population: StateSlot[PopulationSnapshot] = namespace.slot(
            _POPULATION_FILE,
            PopulationSnapshot,
        )
        self._metrics: StateSlot[MetricSpace] = namespace.slot(
            _METRICS_FILE,
            MetricSpace,
        )

    def load_population(self) -> Population:
        """Load the population, starting empty only when it is absent."""
        snapshot = self._population.load_optional()
        return population_from_snapshot(snapshot or PopulationSnapshot())

    def save_population(self, population: Population) -> None:
        """Validate and atomically save the complete population."""
        self._population.save(population_snapshot(population))

    def load_metric_space(self) -> MetricSpace:
        """Load the run's metric space.

        State written before the space was persisted has no document; it loads
        as the empty strict space, which is how those runs already compared.
        """
        return self._metrics.load_optional() or MetricSpace()

    def save_metric_space(self, space: MetricSpace) -> None:
        """Atomically record the axes and tolerance this run selects within."""
        self._metrics.save(space)

    @property
    def namespace(self) -> StateNamespace:
        """Return the namespace for committing its validated snapshot."""
        return self._namespace
