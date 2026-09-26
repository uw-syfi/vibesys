"""Plain orchestration settings and resume compatibility."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.orchestration import OrchestrationResumeDecision
from vs_project.api import OrchestrationDescriptor


class IssueQueueOptions(BaseModel):
    """Resolved plain-loop settings, including its selected issue backend."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    modality: str | None = None
    max_rounds: int = Field(gt=0)
    max_attempts_per_issue: int = Field(gt=0)
    max_issues_per_perf_eval: int = Field(gt=0)
    tracker_backend: Literal["local", "github"] = "local"
    tracker_repository: str | None = None

    @field_validator("tracker_repository")
    @classmethod
    def _validate_repository(cls, value: str | None) -> str | None:
        """Require an unqualified GitHub OWNER/REPOSITORY slug."""
        if value is not None and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
            raise ValueError("tracker_repository must use OWNER/REPOSITORY format")  # noqa: TRY003  # lint-waiver: LW-920427 [TRY003]; state the required external repository syntax.
        return value

    @model_validator(mode="after")
    def _validate_backend(self) -> IssueQueueOptions:
        """Require the repository setting to match the selected backend."""
        if self.tracker_backend == "github" and self.tracker_repository is None:
            raise ValueError("tracker_repository is required when tracker_backend is github")  # noqa: TRY003  # lint-waiver: LW-920428 [TRY003]; name the GitHub backend's required configuration.
        if self.tracker_backend == "local" and self.tracker_repository is not None:
            raise ValueError("tracker_repository is only valid when tracker_backend is github")  # noqa: TRY003  # lint-waiver: LW-920429 [TRY003]; reject remote-only configuration for the local backend.
        return self


def descriptor_from_options(options: IssueQueueOptions) -> OrchestrationDescriptor:
    """Persist plain policy options in the active descriptor format."""
    return OrchestrationDescriptor(id="plain", config_version=1, options=options.model_dump())


def options_from_descriptor(descriptor: OrchestrationDescriptor) -> IssueQueueOptions:
    """Validate a recorded descriptor against the plain-owned schema."""
    if descriptor.id != "plain" or descriptor.config_version != 1:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_invalid",
                stage="resume_resolution",
                message="unsupported plain orchestration descriptor",
            )
        )
    return IssueQueueOptions.model_validate(descriptor.options)


def compare_resume(
    recorded: OrchestrationDescriptor, requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    """Keep plain settings fixed, allowing only an increased total round budget."""
    old = options_from_descriptor(recorded)
    new = options_from_descriptor(requested)
    changed = tuple(
        sorted(
            name
            for name in IssueQueueOptions.model_fields
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
