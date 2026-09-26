"""Typed persistence adapter for the evolutionary loop's durable state.

``EvolveState`` is the single value the loop checkpoints between generations.
``population`` is the running, committed :class:`PopulationState`.
``generation_start`` is a frozen copy of ``population`` taken the moment a
generation begins; it lets ``PopulationSearch.propose`` be replayed
deterministically for every child slot in that generation, on a fresh
process, without depending on anything that happened mid-generation.
``admitted_slots`` counts how many of the generation's child slots (1-indexed,
strictly sequential) have already been evaluated and admitted (or skipped),
so a resumed run only evaluates the slots that come after it.

Between generations (no generation in progress) ``generation_start`` is
``None`` and ``admitted_slots`` is 0; the next generation number is always
``population.generation + 1``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from vibesys.evaluators.metrics import MetricSpace
from vibesys.search.population.models import PopulationState

if TYPE_CHECKING:
    from framework.api import StateNamespace, StateSlot

_STATE_FILE = "state.json"
_METRICS_FILE = "metrics.json"


class EvolveState(BaseModel):
    """The exact durable state one checkpoint commits."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    population: PopulationState
    generation_start: PopulationState | None = None
    admitted_slots: int = Field(default=0, ge=0)


class EvolutionProjection(BaseModel):
    """The committed population and metric space exposed to run readers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    population: PopulationState
    metric_space: MetricSpace
    generation: int


class EvolutionStateStore:
    """Persist a validated :class:`EvolveState` inside one portable namespace."""

    def __init__(self, namespace: StateNamespace) -> None:
        """Bind the adapter to one portable evolve-loop namespace."""
        self._namespace = namespace
        self._state: StateSlot[EvolveState] = namespace.slot(_STATE_FILE, EvolveState)
        self._metrics: StateSlot[MetricSpace] = namespace.slot(_METRICS_FILE, MetricSpace)

    def load(self) -> EvolveState | None:
        """Load the committed state, or ``None`` for a run with no history."""
        return self._state.load_optional()

    def save(self, state: EvolveState) -> None:
        """Validate and atomically save the complete durable state."""
        self._state.save(state)

    def load_metric_space(self) -> MetricSpace:
        """Load the run's metric space.

        State written before the space was persisted has no document; it
        loads as the empty strict space, which is how those runs already
        compared.
        """
        return self._metrics.load_optional() or MetricSpace()

    def save_metric_space(self, space: MetricSpace) -> None:
        """Atomically record the axes and tolerance this run selects within."""
        self._metrics.save(space)

    def projection(self, state: EvolveState) -> EvolutionProjection:
        """Project one durable state into the read model committed readers see."""
        return EvolutionProjection(
            population=state.population,
            metric_space=self.load_metric_space(),
            generation=state.population.generation,
        )

    def checkpoint_writes(self, state: EvolveState) -> dict[str, BaseModel]:
        """Return the validated files that define one durable checkpoint."""
        return {_STATE_FILE: state, _METRICS_FILE: self.load_metric_space()}

    @property
    def namespace(self) -> StateNamespace:
        """Return the namespace for committing its validated snapshot."""
        return self._namespace
