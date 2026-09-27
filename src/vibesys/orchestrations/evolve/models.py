"""Strict configuration and durable state for evolutionary search."""

from __future__ import annotations

from typing import Annotated, Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.roles.common import Verdict
from vibesys.search.population import Individual, OpenEvolveSelectorConfig, PopulationState


class MutatorContext(BaseModel):
    """Inputs for one evolve mutation turn."""

    model_config = ConfigDict(frozen=True)

    accuracy_command: str | None
    benchmark_command: str | None
    domain_implementer: str
    failed_lessons: list[str]
    inspirations: list[Individual]
    interface: str
    is_cold_start: bool
    modality: str | None
    num_failed_attempts: int
    objective: str
    objectives: list[Objective] | None
    parent: Individual | None
    reference_path: str
    repair_seed: bool
    runtime_notes: str

    @model_validator(mode="after")
    def _require_parent_unless_cold_start(self) -> Self:
        if not self.is_cold_start and self.parent is None:
            message = "parent is required unless is_cold_start is True"
            raise ValueError(message)
        return self


class MutatorResponse(BaseModel):
    """Mutation rationale recorded on the resulting population member."""

    summary: str = Field(description="Short description of the change made to the parent.")
    hypothesis: str = Field(
        description="Why this change is expected to improve the headline metric.",
    )
    expected_behavior: str = Field(description="Observable change a reviewer should expect.")


class CandidateJudgeContext(BaseModel):
    """Inputs for one independent evolve candidate review."""

    model_config = ConfigDict(frozen=True)

    accuracy_command: str | None
    benchmark_command: str | None
    domain_judge: str
    interface: str
    modality: str | None
    objective: str | None
    pass_criteria: str
    runtime_notes: str


class JudgeResponse(BaseModel):
    """Review decision for one evolve candidate."""

    analysis: str = Field(description="Detailed analysis of the candidate implementation.")
    feedback: str = Field(
        description="Specific actionable feedback for the mutator. Empty string if passing."
    )
    verdict: Verdict = Field(description="PASS if all criteria are met, FAIL otherwise.")


class CandidateProfilerContext(BaseModel):
    """Inputs shared by all evolve candidate profiler kinds."""

    model_config = ConfigDict(frozen=True)

    benchmark_command: str | None
    domain_profiler: str
    modality: str | None
    objective: str | None
    pareto_objectives_addendum: str
    profile_execution: str
    profile_focus: str
    profiler_mcp_name: str
    profiler_support_name: str
    runtime_notes: str


class OpenEvolveOverrides(TypedDict, total=False):
    """CLI scalar overrides before evolve resolves persisted defaults."""

    openevolve_population_size: int | None
    openevolve_archive_size: int | None
    openevolve_num_islands: int | None
    openevolve_migration_interval: int | None
    openevolve_migration_rate: float | None


def resolve_openevolve_options(
    search_policy: str | None, values: OpenEvolveOverrides
) -> tuple[str | None, dict[str, int | float | None]]:
    """Resolve CLI overrides against the evolve-owned search defaults."""
    population_size = values.get("openevolve_population_size")
    archive_size = values.get("openevolve_archive_size")
    num_islands = values.get("openevolve_num_islands")
    migration_interval = values.get("openevolve_migration_interval")
    migration_rate = values.get("openevolve_migration_rate")
    if all(
        value is None
        for value in (
            population_size,
            archive_size,
            num_islands,
            migration_interval,
            migration_rate,
        )
    ):
        return search_policy, {
            "openevolve_population_size": None,
            "openevolve_archive_size": None,
            "openevolve_num_islands": None,
            "openevolve_migration_interval": None,
            "openevolve_migration_rate": None,
        }
    defaults = OpenEvolveSelectorConfig()
    config = OpenEvolveSelectorConfig(
        population_size=population_size or defaults.population_size,
        archive_size=archive_size or defaults.archive_size,
        num_islands=num_islands or defaults.num_islands,
        migration_interval=migration_interval or defaults.migration_interval,
        migration_rate=migration_rate if migration_rate is not None else defaults.migration_rate,
    )
    resolved = {
        "openevolve_population_size": config.population_size,
        "openevolve_archive_size": config.archive_size,
        "openevolve_num_islands": config.num_islands,
        "openevolve_migration_interval": config.migration_interval,
        "openevolve_migration_rate": config.migration_rate,
    }
    return search_policy or "openevolve", resolved


class EvolveOptions(BaseModel):
    """Resolved evolutionary-search policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    modality: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    max_generations: Annotated[int, Field(gt=0)]
    children_per_generation: Annotated[int, Field(gt=0)]
    k_top_inspirations: Annotated[int, Field(ge=0)]
    k_random_inspirations: Annotated[int, Field(ge=0)]
    selection_temperature: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    seed: int | None = None
    search_policy: Literal["vibesys", "openevolve"] | None = None
    openevolve_population_size: Annotated[int, Field(gt=0)] | None = None
    openevolve_archive_size: Annotated[int, Field(gt=0)] | None = None
    openevolve_num_islands: Annotated[int, Field(gt=0)] | None = None
    openevolve_migration_interval: Annotated[int, Field(gt=0)] | None = None
    openevolve_migration_rate: Annotated[float, Field(ge=0, le=1)] | None = None
    frontier_bias: Annotated[float, Field(ge=0, le=1)]
    bootstrap_max_attempts: Annotated[int, Field(gt=0)]
    keep_deployments: Literal[False]
    max_parallelism: Annotated[int, Field(gt=0)]
    metric_space: MetricSpace = Field(default_factory=MetricSpace)

    @model_validator(mode="after")
    def _validate_selector(self) -> Self:
        configured = self.openevolve_config() is not None
        if self.search_policy == "vibesys" and configured:
            message = "OpenEvolve settings require search_policy='openevolve'"
            raise ValueError(message)
        return self

    def openevolve_config(self) -> OpenEvolveSelectorConfig | None:
        """Return the configured OpenEvolve policy, if any knob selected it."""
        values = (
            self.openevolve_population_size,
            self.openevolve_archive_size,
            self.openevolve_num_islands,
            self.openevolve_migration_interval,
            self.openevolve_migration_rate,
        )
        if all(value is None for value in values):
            return None
        defaults = OpenEvolveSelectorConfig()
        return OpenEvolveSelectorConfig(
            population_size=self.openevolve_population_size or defaults.population_size,
            archive_size=self.openevolve_archive_size or defaults.archive_size,
            num_islands=self.openevolve_num_islands or defaults.num_islands,
            migration_interval=(self.openevolve_migration_interval or defaults.migration_interval),
            migration_rate=(
                self.openevolve_migration_rate
                if self.openevolve_migration_rate is not None
                else defaults.migration_rate
            ),
        )


class EvolveState(BaseModel):
    """Complete crash-recoverable evolutionary-search aggregate."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    population: PopulationState
    metric_space: MetricSpace = Field(default_factory=MetricSpace)
    generation_start: PopulationState | None = None
    admitted_slots: int = Field(default=0, ge=0)


__all__ = [
    "CandidateJudgeContext",
    "CandidateProfilerContext",
    "EvolveOptions",
    "EvolveState",
    "JudgeResponse",
    "MutatorContext",
    "MutatorResponse",
    "resolve_openevolve_options",
]
