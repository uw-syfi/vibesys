"""Pure, serializable data types for evolutionary population search.

Every type here is a frozen pydantic model: no method mutates in place, and
every field is plain data (no ``RunContext``, agent handle, or filesystem
path). :class:`PopulationState` is the single value orchestration persists
and reloads between rounds; :class:`PopulationSearch` (in ``search.py``) is
the only thing that reads or produces one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vibesys.evaluators.metrics import MetricSpace

__all__ = [
    "CandidateOutcome",
    "Individual",
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


class Individual(BaseModel):
    """One candidate program in the population.

    ``commit`` is a git SHA in the workspace repo, set by orchestration once a
    candidate is materialized on disk; search/population never reads or
    writes the filesystem. Failed offspring are retained (``passed=False``,
    ``commit=None``) so ``failure_lessons`` can surface their feedback.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: int = Field(gt=0)
    generation: int = Field(ge=0)
    parent_id: int | None = None
    inspiration_ids: tuple[int, ...] = ()
    commit: str | None = None
    perf_metric: float | None = Field(default=None, allow_inf_nan=False)
    perf_unit: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    passed: bool = False
    summary: str = ""
    feedback: str = ""
    policy_parent_id: str | None = None
    policy_target_island: int | None = None


class CandidateOutcome(BaseModel):
    """One evaluated candidate, before a population id is assigned.

    ``code`` is the candidate's canonical multi-file patch; orchestration
    supplies it only when ``PopulationSearch.needs_code`` is true (the
    OpenEvolve selector) since search/population cannot read git itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    passed: bool
    parent_id: int | None
    inspiration_ids: tuple[int, ...] = ()
    summary: str = ""
    feedback: str | None = None
    commit: str | None = None
    perf_metric: float | None = Field(default=None, allow_inf_nan=False)
    perf_unit: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    policy_parent_id: str | None = None
    target_island: int | None = None
    code: str | None = None


class Proposal(BaseModel):
    """A materializable selection plus selector-specific lineage data."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    parent: Individual
    inspirations: tuple[Individual, ...] = ()
    policy_parent_id: str | None = None
    target_island: int | None = None


class OpenEvolveSelectorConfig(BaseModel):
    """Supported OpenEvolve database knobs, pinned to v0.3.1 semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    population_size: int = Field(default=1000, gt=0)
    archive_size: int = Field(default=100, gt=0)
    num_islands: int = Field(default=5, gt=0)
    migration_interval: int = Field(default=50, gt=0)
    migration_rate: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)


class _PersistedObjective(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    direction: Literal["max", "min"]


class OpenEvolveSelectorState(BaseModel):
    """OpenEvolve database state, held entirely as data.

    ``files`` mirrors the relative-path -> text-content shape OpenEvolve's own
    ``ProgramDatabase.save``/``load`` write to a directory, but held in memory
    as part of :class:`PopulationState` instead of a directory on disk. Each
    admit *replaces* this field wholesale (the field always reflects the
    complete current database), so state size tracks the live database size
    (bounded by ``population_size``/``archive_size``), never the number of
    admits over a run's lifetime.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    config: OpenEvolveSelectorConfig
    objective_signature: tuple[_PersistedObjective, ...] = ()
    admitted_individual_ids: tuple[int, ...] = ()
    current_island: int = Field(default=0, ge=0)
    rng_state: RandomState
    files: dict[str, str] = Field(default_factory=dict)


class PopulationConfig(BaseModel):
    """Deterministic knobs a run needs to reproduce identical selections."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    space: MetricSpace = Field(default_factory=MetricSpace)
    selection_temperature: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    frontier_bias: float = Field(default=0.7, ge=0, le=1)
    k_top_inspirations: int = Field(default=0, ge=0)
    k_random_inspirations: int = Field(default=0, ge=0)
    selector: Literal["vibesys", "openevolve"] = "vibesys"
    openevolve: OpenEvolveSelectorConfig | None = None
    seed: int | None = None


class PopulationState(BaseModel):
    """The single durable value orchestration checkpoints between rounds.

    ``rng_state`` seeds ``PopulationSearch.propose``'s sampling
    deterministically: given the same state, ``propose`` always returns the
    same proposal. Orchestration never mutates this value directly; it only
    replaces it with whatever ``PopulationSearch`` returns.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    individuals: tuple[Individual, ...] = ()
    next_id: int = Field(default=1, gt=0)
    generation: int = Field(default=0, ge=0)
    rng_state: RandomState
    selector_state: OpenEvolveSelectorState | None = None
