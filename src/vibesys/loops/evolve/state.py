"""Typed persistence adapter for evolutionary population state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.evolve.population import Individual, Population
from vs_loop_state.api import IndividualRecord, PopulationSnapshot

if TYPE_CHECKING:
    from vs_project.api import GitTracker, Project, StateNamespace, StateSlot

_POPULATION_FILE = "population.json"
_METRICS_FILE = "metrics.json"
_CURSOR_FILE = "generation.json"


class EvolveResumeError(RuntimeError):
    """A paid or recorded child cannot be safely replayed from available state."""


class CandidatePlanRecord(BaseModel):
    """Stable selection saved after the sampler checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    parent_id: int
    inspiration_ids: tuple[int, ...]
    policy_parent_id: str | None = None
    target_island: int | None = None


class CandidateOutcomeRecord(BaseModel):
    """Paid evaluation result saved before population admission."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    passed: bool
    parent_id: int | None
    inspiration_ids: tuple[int, ...]
    summary: str
    feedback: str | None
    commit: str | None = None
    perf_metric: float | None = None
    perf_unit: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    policy_parent_id: str | None = None
    target_island: int | None = None


class GenerationJournal(BaseModel):
    """One generation's durable child slots and replay boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    generation: int = Field(gt=0)
    mode: Literal["serial", "parallel"]
    phase: Literal["ready", "planning", "planned", "evaluating", "recording"] = "ready"
    next_child: int = Field(default=1, gt=0)
    plans: dict[int, CandidatePlanRecord] = Field(default_factory=dict)
    outcomes: dict[int, CandidateOutcomeRecord] = Field(default_factory=dict)
    recorded: dict[int, int] = Field(default_factory=dict)


class GenerationCursor(BaseModel):
    """Committed budget and any generation still being processed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    completed: int = Field(default=0, ge=0)
    active: GenerationJournal | None = None
    rng_state: tuple[int, tuple[int, ...], float | None] | None = None


def restore_uncommitted_evolve_state(project: Project, git: GitTracker, run_id: str) -> None:
    """Discard only a cursor that did not commit before resumed setup writes.

    Bootstrap can leave uncommitted population data after paid work, so any
    other changed evolve file makes recovery ambiguous. A committed planning
    or evaluating marker remains unsafe even if only its cursor is dirty.
    """
    directory = project.state.portable_namespace(run_id, "evolve").external_directory()
    namespace = directory.relative_to(project.root).as_posix()
    cursor_path = f"{namespace}/{_CURSOR_FILE}"
    other_changes = git.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--ignored",
            "--untracked-files=all",
            "--",
            namespace,
            f":(exclude){cursor_path}",
        ]
    )
    if other_changes.stdout:
        raise EvolveResumeError(  # noqa: TRY003
            "uncommitted evolve state beyond the generation cursor may contain paid work; "
            "resuming would risk replaying it"
        )
    cursor_changes = git.run(
        ["git", "status", "--porcelain=v1", "--ignored", "--untracked-files=all", "--", cursor_path]
    )
    if not cursor_changes.stdout:
        return
    committed = git.run(["git", "show", f"HEAD:{cursor_path}"], check=False)
    if committed.returncode != 0:
        raise EvolveResumeError("evolve has no committed generation cursor to restore")  # noqa: TRY003
    cursor = GenerationCursor.model_validate_json(committed.stdout)
    if cursor.active is not None and cursor.active.phase in {"planning", "evaluating"}:
        raise EvolveResumeError(  # noqa: TRY003
            f"committed evolve generation {cursor.active.generation} stopped during "
            f"{cursor.active.phase}; paid work may have started"
        )
    git.run(["git", "reset", "--quiet", "HEAD", "--", cursor_path])
    git.run(["git", "restore", "--source=HEAD", "--worktree", "--", cursor_path])


class EvolutionProjection(BaseModel):
    """The committed population and metric space exposed to run readers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    population: PopulationSnapshot
    metric_space: MetricSpace
    generation: GenerationCursor


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
        self._cursor: StateSlot[GenerationCursor] = namespace.slot(
            _CURSOR_FILE,
            GenerationCursor,
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

    def completed_generation(self) -> int:
        """Return the last fully checkpointed generation."""
        return (self._cursor.load_optional() or GenerationCursor()).completed

    def load_cursor(self) -> GenerationCursor | None:
        """Read the committed budget cursor, including an active generation."""
        return self._cursor.load_optional()

    def save_cursor(self, cursor: GenerationCursor) -> None:
        """Stage the initial cursor before bootstrap can consume paid work."""
        self._cursor.save(cursor)

    def save_journal(
        self,
        completed: int,
        journal: GenerationJournal,
        rng_state: tuple[int, tuple[int, ...], float | None],
    ) -> None:
        """Stage a child-slot transition for the next exact-state commit."""
        self._cursor.save(
            GenerationCursor(completed=completed, active=journal, rng_state=rng_state)
        )

    def save_completed_generation(
        self, generation: int, rng_state: tuple[int, tuple[int, ...], float | None]
    ) -> None:
        """Stage a completed generation with its search-policy state."""
        self._cursor.save(GenerationCursor(completed=generation, rng_state=rng_state))

    def projection(self) -> EvolutionProjection:
        """Read the same validated files committed by the policy."""
        return EvolutionProjection(
            population=self._population.load_optional() or PopulationSnapshot(),
            metric_space=self.load_metric_space(),
            generation=self._cursor.load_optional() or GenerationCursor(),
        )

    @property
    def namespace(self) -> StateNamespace:
        """Return the namespace for committing its validated snapshot."""
        return self._namespace
