"""Validated, versioned options shared by the four built-in agent orchestrators."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.orchestration import OrchestrationResumeDecision
from vs_project.api import OrchestrationDescriptor

if TYPE_CHECKING:
    from vibesys.evaluators.input_manifest import BenchmarkResult

AGENT_CONFIG_VERSION = 1
PortableText = Annotated[str, Field(min_length=1, max_length=256)]


class UnsupportedAgentOrchestrationError(ValueError):
    """A descriptor names an unsupported agent policy or option version."""

    def __init__(self, orchestration_id: str, version: int) -> None:
        """Identify the offending ID and configuration version."""
        super().__init__(
            f"Unsupported agent orchestration {orchestration_id!r} configuration version {version}"
        )


class AgentOrchestrationOptions(BaseModel):
    """Strict execution options common to the four agent policy classes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    interface: PortableText
    modality: PortableText | None = None
    max_rounds: Annotated[int, Field(gt=0)]
    max_retries_per_round: Annotated[int, Field(gt=0)]
    judge_every: Annotated[int, Field(gt=0)]
    official_eval_every: Annotated[int, Field(gt=0)]
    memory_layout: PortableText
    operator_constraints: tuple[str, ...] = ()
    metric_space: MetricSpace = Field(default_factory=MetricSpace)
    profile_guided: ProfileGuidedInput | None = None


def recorded_metric_space(
    metrics: MetricSpace, benchmark_result: BenchmarkResult | None
) -> MetricSpace:
    """Include the benchmark axis without losing task noise tolerance."""
    if benchmark_result is None or metrics.axis(benchmark_result.metric) is not None:
        return metrics
    return metrics.model_copy(
        update={
            "objectives": (*metrics.objectives, Objective(benchmark_result.metric, "max")),
        }
    )


def descriptor_from_options(
    options: AgentOrchestrationOptions, *, orchestration_id: str
) -> OrchestrationDescriptor:
    """Persist one canonical policy identity and its validated options."""
    return OrchestrationDescriptor(
        id=orchestration_id,
        config_version=AGENT_CONFIG_VERSION,
        options=options.model_dump(mode="json"),
    )


def options_from_descriptor(descriptor: OrchestrationDescriptor) -> AgentOrchestrationOptions:
    """Reject unknown option versions and keys before opening resources."""
    if descriptor.config_version != AGENT_CONFIG_VERSION:
        raise UnsupportedAgentOrchestrationError(descriptor.id, descriptor.config_version)
    return AgentOrchestrationOptions.model_validate_json(json.dumps(descriptor.options))


def compare_resume_descriptors(
    recorded: OrchestrationDescriptor,
    requested: OrchestrationDescriptor,
) -> OrchestrationResumeDecision:
    """Allow only a larger total round budget for the same agent policy."""
    recorded_options = options_from_descriptor(recorded)
    requested_options = options_from_descriptor(requested)
    changed = [
        name
        for name in AgentOrchestrationOptions.model_fields
        if name != "max_rounds"
        and getattr(recorded_options, name) != getattr(requested_options, name)
    ]
    if recorded.id != requested.id:
        changed.insert(0, "orchestration.id")
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
    if requested_options.max_rounds < recorded_options.max_rounds:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=(
                    "max_rounds is the run's total limit and cannot decrease when resuming "
                    f"(recorded {recorded_options.max_rounds}, "
                    f"requested {requested_options.max_rounds})"
                ),
            )
        )
    if requested_options.max_rounds > recorded_options.max_rounds:
        return OrchestrationResumeDecision(
            descriptor=requested,
            requires_clean_workspace=True,
        )
    return OrchestrationResumeDecision(descriptor=None)
