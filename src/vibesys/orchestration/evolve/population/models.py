"""Pure, serializable data types for evolutionary population search.

Every type here is a frozen pydantic model: no method mutates in place, and
every field is plain data (no runtime capability, agent handle, or filesystem
path). :class:`PopulationState` is the single value orchestration persists
and reloads between rounds; :class:`PopulationSearch` (in ``search.py``) is
the only thing that reads or produces one.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from vibesys.metrics import MetricSpace
from vs_core.api import Value

__all__ = [
    "CandidateOutcome",
    "Individual",
    "Metrics",
    "OpenEvolveSelectorConfig",
    "OpenEvolveSelectorState",
    "PopulationConfig",
    "PopulationState",
    "Proposal",
    "RandomState",
]

# ``random.Random.getstate()`` / ``setstate()`` shape: version, internal
# Mersenne Twister state tuple, and an optional gauss cache value.
RandomState = tuple[int, tuple[int, ...], float | None]

type Finite = Annotated[float, Field(allow_inf_nan=False)]
# A metric row as name and value pairs in report order. A mapping would be mutable state, and the
# population is a value the strategy's committed state holds, so it holds only tuples.
type Metrics = tuple[tuple[str, Finite], ...]


class Individual(Value):
    """One candidate program in the population.

    ``commit`` is a git SHA in the workspace repo, set by the evolve orchestrator once a
    candidate is materialized on disk; population policy never reads or
    writes the filesystem. Failed offspring are retained (``passed=False``,
    ``commit=None``) so ``failure_lessons`` can surface their feedback.
    """

    id: int = Field(gt=0)
    generation: int = Field(ge=0)
    parent_id: int | None = None
    inspiration_ids: tuple[int, ...] = ()
    commit: str | None = None
    perf_metric: float | None = Field(default=None, allow_inf_nan=False)
    perf_unit: str | None = None
    metrics: Metrics = ()
    passed: bool = False
    summary: str = ""
    feedback: str = ""
    policy_parent_id: str | None = None
    policy_target_island: int | None = None

    def metric_map(self) -> dict[str, float]:
        """The metric row by name (a later pair of the same name wins, as in a mapping)."""
        return dict(self.metrics)


class CandidateOutcome(Value):
    """One evaluated candidate, before a population id is assigned.

    ``code`` is the candidate's canonical multi-file patch; the evolve orchestrator
    supplies it only when ``PopulationSearch.needs_code`` is true (the
    OpenEvolve selector) since population policy cannot read git itself.
    """

    passed: bool
    parent_id: int | None
    inspiration_ids: tuple[int, ...] = ()
    summary: str = ""
    feedback: str | None = None
    commit: str | None = None
    perf_metric: float | None = Field(default=None, allow_inf_nan=False)
    perf_unit: str | None = None
    metrics: Metrics = ()
    policy_parent_id: str | None = None
    target_island: int | None = None
    code: str | None = None


class Proposal(Value):
    """A materializable selection plus selector-specific lineage data."""

    parent: Individual
    inspirations: tuple[Individual, ...] = ()
    policy_parent_id: str | None = None
    target_island: int | None = None


class OpenEvolveSelectorConfig(Value):
    """Supported OpenEvolve database knobs, pinned to v0.3.1 semantics."""

    population_size: int = Field(default=1000, gt=0)
    archive_size: int = Field(default=100, gt=0)
    num_islands: int = Field(default=5, gt=0)
    migration_interval: int = Field(default=50, gt=0)
    migration_rate: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)


class _PersistedObjective(Value):
    name: str
    direction: Literal["max", "min"]


class OpenEvolveSelectorState(Value):
    """OpenEvolve database state, held entirely as data.

    ``files`` mirrors the relative-path -> text-content shape OpenEvolve's own
    ``ProgramDatabase.save``/``load`` write to a directory, but held in memory
    as part of :class:`PopulationState` instead of a directory on disk, as pairs
    in file order. Each admit *replaces* this field wholesale (the field always
    reflects the complete current database), so state size tracks the live
    database size (bounded by ``population_size``/``archive_size``), never the
    number of admits over a run's lifetime.
    """

    schema_version: Literal[1] = 1
    config: OpenEvolveSelectorConfig
    objective_signature: tuple[_PersistedObjective, ...] = ()
    admitted_individual_ids: tuple[int, ...] = ()
    current_island: int = Field(default=0, ge=0)
    rng_state: RandomState
    files: tuple[tuple[str, str], ...] = ()


class PopulationConfig(Value):
    """Deterministic knobs a run needs to reproduce identical selections."""

    space: MetricSpace = Field(default_factory=MetricSpace)
    selection_temperature: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    frontier_bias: float = Field(default=0.7, ge=0, le=1)
    k_top_inspirations: int = Field(default=0, ge=0)
    k_random_inspirations: int = Field(default=0, ge=0)
    selector: Literal["vibesys", "openevolve"] = "vibesys"
    openevolve: OpenEvolveSelectorConfig | None = None
    seed: int | None = None


class PopulationState(Value):
    """The single durable value orchestration checkpoints between rounds.

    ``rng_state`` seeds ``PopulationSearch.propose``'s sampling
    deterministically: given the same state, ``propose`` always returns the
    same proposal. Orchestration never mutates this value directly; it only
    replaces it with whatever ``PopulationSearch`` returns.
    """

    individuals: tuple[Individual, ...] = ()
    next_id: int = Field(default=1, gt=0)
    generation: int = Field(default=0, ge=0)
    rng_state: RandomState
    selector_state: OpenEvolveSelectorState | None = None
