"""Versioned configuration and resume policy for evolutionary orchestration."""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.evaluators.metrics import MetricSpace
from vibesys.orchestration import OrchestrationResumeDecision
from vs_project.api import OrchestrationDescriptor

PortableText = Annotated[str, Field(min_length=1, max_length=256)]


class _OpenEvolveSettingsError(ValueError):
    def __init__(self) -> None:
        super().__init__("OpenEvolve settings require search_policy='openevolve'")


class EvolveOptions(BaseModel):
    """Resolved, sanitized settings for one evolutionary run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    modality: PortableText | None = None
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
    keep_deployments: bool
    max_parallelism: Annotated[int, Field(gt=0)]
    metric_space: MetricSpace = Field(default_factory=MetricSpace)

    @model_validator(mode="after")
    def _validate_search_policy_settings(self) -> Self:
        openevolve_values = (
            self.openevolve_population_size,
            self.openevolve_archive_size,
            self.openevolve_num_islands,
            self.openevolve_migration_interval,
            self.openevolve_migration_rate,
        )
        if self.search_policy == "vibesys" and any(
            value is not None for value in openevolve_values
        ):
            raise _OpenEvolveSettingsError
        return self


def descriptor_from_options(options: EvolveOptions) -> OrchestrationDescriptor:
    """Persist the validated evolutionary-search settings."""
    return OrchestrationDescriptor(
        id="evolve", config_version=1, options=options.model_dump(mode="json")
    )


def options_from_descriptor(descriptor: OrchestrationDescriptor) -> EvolveOptions:
    """Validate a recorded descriptor before using its version 1 settings."""
    if descriptor.id != "evolve" or descriptor.config_version != 1:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_invalid",
                stage="resume_resolution",
                message="unsupported evolve orchestration descriptor",
            )
        )
    return EvolveOptions.model_validate_json(json.dumps(descriptor.options), strict=True)


def compare_resume(
    recorded: OrchestrationDescriptor,
    requested: OrchestrationDescriptor,
) -> OrchestrationResumeDecision:
    """Allow only an increased generation budget for a version 4 evolve run."""
    old = options_from_descriptor(recorded)
    new = options_from_descriptor(requested)
    changed = tuple(
        sorted(
            field
            for field in EvolveOptions.model_fields
            if field != "max_generations" and getattr(old, field) != getattr(new, field)
        )
    )
    if changed:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=(
                    "resuming a run cannot change its recorded configuration "
                    f"fields: {', '.join(changed)}"
                ),
            )
        )
    if new.max_generations < old.max_generations:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=(
                    "max_generations is the run's total limit and cannot decrease when "
                    "resuming "
                    f"(recorded {old.max_generations}, requested {new.max_generations})"
                ),
            )
        )
    if new.max_generations == old.max_generations:
        return OrchestrationResumeDecision(descriptor=None)
    return OrchestrationResumeDecision(descriptor=requested, requires_clean_workspace=True)
