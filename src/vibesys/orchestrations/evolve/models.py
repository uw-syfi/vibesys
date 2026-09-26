"""Strict configuration and durable state for evolutionary search."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibesys.evaluators.metrics import MetricSpace
from vibesys.search.population import OpenEvolveSelectorConfig, PopulationState


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


__all__ = ["EvolveOptions", "EvolveState"]
