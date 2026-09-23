"""Plain orchestration settings and resume compatibility."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.orchestration import (
    OrchestrationResumeDecision,
    ResumeConfigSnapshot,
    ResumeProjection,
)
from vs_project.api import (
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    PlainRunConfiguration,
    RunEnvironmentRecord,
)


class PlainOrchestrationOptions(BaseModel):
    """Resolved, strict plain-loop settings persisted in a v4 descriptor."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model: str | None = None
    agent_backend: str
    agent_driver: str | None = None
    cli_provider: str | None = None
    cli_timeout: int | None = Field(default=None, gt=0)
    compute_backend: str
    profiler: str | None = None
    modality: str | None = None
    default_reasoning_effort: str | None = None
    outer_model: str | None = None
    outer_reasoning_effort: str | None = None
    inner_model: str | None = None
    inner_reasoning_effort: str | None = None
    max_rounds: int = Field(gt=0)
    max_attempts_per_issue: int = Field(gt=0)
    max_issues_per_perf_eval: int = Field(gt=0)

    @classmethod
    def from_legacy(cls, configuration: PlainRunConfiguration, *, profiler: str) -> Self:
        """Build v4 options from the loop's transitional v3 configuration."""
        values = configuration.model_dump(exclude={"outer_loop", "run_environment"})
        values["profiler"] = profiler
        return cls.model_validate(values)


def descriptor_from_configuration(
    configuration: PlainRunConfiguration, *, profiler: str
) -> OrchestrationDescriptor:
    """Adapt a version 3 fixture to the owner-native descriptor builder."""
    options = PlainOrchestrationOptions.from_legacy(configuration, profiler=profiler)
    return descriptor_from_options(options, profiler=profiler)


def descriptor_from_options(
    options: PlainOrchestrationOptions, *, profiler: str
) -> OrchestrationDescriptor:
    """Persist resolved plain options without constructing version 3 settings."""
    resolved = options.model_copy(update={"profiler": profiler})
    return OrchestrationDescriptor(id="plain", config_version=1, options=resolved.model_dump())


def legacy_configuration_from_options(
    options: PlainOrchestrationOptions,
    *,
    run_environment: RunEnvironmentRecord,
    profiler: str,
) -> PlainRunConfiguration:
    """Construct version 3 settings only when resuming an old run."""
    return PlainRunConfiguration.model_validate(
        {
            **options.model_dump(),
            "outer_loop": "plain",
            "run_environment": run_environment,
            "profiler": profiler,
        }
    )


def options_from_descriptor(descriptor: OrchestrationDescriptor) -> PlainOrchestrationOptions:
    """Validate a recorded descriptor against the plain-owned schema."""
    if descriptor.id != "plain" or descriptor.config_version != 1:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_invalid",
                stage="resume_resolution",
                message="unsupported plain orchestration descriptor",
            )
        )
    return PlainOrchestrationOptions.model_validate(descriptor.options)


def compare_resume(
    recorded: OrchestrationDescriptor, requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    """Keep plain settings fixed, allowing only an increased total round budget."""
    old = options_from_descriptor(recorded)
    new = options_from_descriptor(requested)
    changed = tuple(
        sorted(
            name
            for name in PlainOrchestrationOptions.model_fields
            if name != "max_rounds" and getattr(old, name) != getattr(new, name)
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
    if new.max_rounds < old.max_rounds:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=(
                    "max_rounds is the run's total limit and cannot decrease when resuming "
                    f"(recorded {old.max_rounds}, requested {new.max_rounds})"
                ),
            )
        )
    if new.max_rounds == old.max_rounds:
        return OrchestrationResumeDecision(descriptor=None)
    return OrchestrationResumeDecision(descriptor=requested, requires_clean_workspace=True)


def resume_projection(manifest: OrchestrationRunManifest) -> ResumeProjection:
    """Project validated v4 plain settings directly into CLI resume values."""
    options = options_from_descriptor(manifest.orchestration)
    return ResumeProjection(
        orchestration_id="plain",
        run_environment=manifest.run_environment,
        config=ResumeConfigSnapshot.model_validate(options.model_dump()),
        cli_values={
            "agent_backend": options.agent_backend,
            "cli_provider": options.cli_provider,
            "backend": options.compute_backend,
            "profiler": options.profiler,
            "modality": options.modality,
            "max_attempts_per_issue": options.max_attempts_per_issue,
            "max_issues_per_perf_eval": options.max_issues_per_perf_eval,
        },
        budget_destination="max_rounds",
        budget_value=options.max_rounds,
    )
