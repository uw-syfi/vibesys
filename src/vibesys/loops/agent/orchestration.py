"""Persisted configuration and resume policy for the agent orchestrations.

Version 4 stores this policy as an opaque descriptor. ``vs_project`` only
validates its JSON shape; these settings and their meaning belong here.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.orchestration import (
    OrchestrationResumeDecision,
    ResumeConfigSnapshot,
    ResumeProjection,
)
from vs_project.api import (
    AgentRunConfiguration,
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    RunEnvironmentRecord,
)

if TYPE_CHECKING:
    from vibesys.evaluators.input_manifest import BenchmarkResult
    from vibesys.loops.metrics import MetricSpace

AGENT_CONFIG_VERSION = 1
AGENT_ORCHESTRATION_IDS = frozenset({"agent", "profile-guided"})
PortableText = Annotated[str, Field(min_length=1, max_length=256)]


class UnsupportedAgentOrchestrationError(ValueError):
    """A run descriptor names an unknown agent policy or version."""

    def __init__(self, orchestration_id: str, version: int) -> None:
        """Name the descriptor identity and unsupported config version."""
        super().__init__(
            f"Unsupported agent orchestration {orchestration_id!r} configuration version {version}"
        )


def recorded_objectives(
    metrics: MetricSpace, benchmark_result: BenchmarkResult | None
) -> tuple[str, ...]:
    """Record frontier axes plus the framework benchmark's scalar axis."""
    axes: dict[str, Literal["max", "min"]] = {
        objective.name: objective.direction for objective in metrics.objectives
    }
    if benchmark_result is not None:
        axes.setdefault(benchmark_result.metric, "max")
    return tuple(f"{name}:{direction}" for name, direction in axes.items())


class AgentOrchestrationOptions(BaseModel):
    """Strict version 1 agent policy options, excluding shared run environment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    inner_loop: PortableText
    interface: PortableText
    model: PortableText | None = None
    agent_backend: PortableText
    agent_driver: PortableText | None = None
    cli_provider: PortableText | None = None
    cli_timeout: Annotated[int, Field(gt=0)] | None = None
    compute_backend: PortableText
    profiler: PortableText | None = None
    modality: PortableText | None = None
    default_reasoning_effort: PortableText | None = None
    outer_model: PortableText | None = None
    outer_reasoning_effort: PortableText | None = None
    inner_model: PortableText | None = None
    inner_reasoning_effort: PortableText | None = None
    max_rounds: Annotated[int, Field(gt=0)]
    max_retries_per_round: Annotated[int, Field(gt=0)]
    judge_every: Annotated[int, Field(gt=0)]
    official_eval_every: Annotated[int, Field(gt=0)]
    memory_layout: PortableText
    operator_constraints: tuple[str, ...] = ()
    objectives: tuple[PortableText, ...] = ()


def descriptor_from_configuration(configuration: AgentRunConfiguration) -> OrchestrationDescriptor:
    """Adapt a version 3 fixture to the owner-native descriptor builder."""
    options = AgentOrchestrationOptions.model_validate(
        configuration.model_dump(exclude={"outer_loop", "run_environment"})
    )
    return descriptor_from_options(options, outer_loop=configuration.outer_loop)


def descriptor_from_options(
    options: AgentOrchestrationOptions,
    *,
    outer_loop: Literal["agent", "profile-guided"],
    profiler: str | None = None,
) -> OrchestrationDescriptor:
    """Persist resolved agent options without constructing version 3 settings."""
    resolved = (
        options.model_copy(update={"profiler": profiler}) if profiler is not None else options
    )
    return OrchestrationDescriptor(
        id=outer_loop,
        config_version=AGENT_CONFIG_VERSION,
        options=resolved.model_dump(mode="json"),
    )


def legacy_configuration_from_options(
    options: AgentOrchestrationOptions,
    *,
    outer_loop: Literal["agent", "profile-guided"],
    run_environment: RunEnvironmentRecord,
    profiler: str,
) -> AgentRunConfiguration:
    """Construct version 3 settings only when resuming an old run."""
    return AgentRunConfiguration.model_validate(
        {
            **options.model_dump(),
            "outer_loop": outer_loop,
            "run_environment": run_environment,
            "profiler": profiler,
        }
    )


def options_from_descriptor(descriptor: OrchestrationDescriptor) -> AgentOrchestrationOptions:
    """Reject unknown IDs, versions, and options before interpreting them."""
    if (
        descriptor.id not in AGENT_ORCHESTRATION_IDS
        or descriptor.config_version != AGENT_CONFIG_VERSION
    ):
        raise UnsupportedAgentOrchestrationError(descriptor.id, descriptor.config_version)
    return AgentOrchestrationOptions.model_validate_json(json.dumps(descriptor.options))


def compare_resume_descriptors(
    recorded: OrchestrationDescriptor,
    requested: OrchestrationDescriptor,
) -> OrchestrationResumeDecision:
    """Allow only a larger total round budget for a resumed v4 agent run."""
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


def resume_projection(manifest: OrchestrationRunManifest) -> ResumeProjection:
    """Project validated agent options directly into CLI resume settings."""
    options = options_from_descriptor(manifest.orchestration)
    return ResumeProjection(
        orchestration_id=manifest.orchestration.id,
        run_environment=manifest.run_environment,
        config=ResumeConfigSnapshot.model_validate(options.model_dump()),
        cli_values={
            "agent_backend": options.agent_backend,
            "cli_provider": options.cli_provider,
            "backend": options.compute_backend,
            "profiler": options.profiler,
            "modality": options.modality,
            "inner_loop": options.inner_loop,
            "interface": options.interface,
            "max_retries_per_round": options.max_retries_per_round,
            "judge_every": options.judge_every,
            "official_eval_every": options.official_eval_every,
            "memory_layout": options.memory_layout,
            "constraint": options.operator_constraints,
        },
        budget_destination="max_rounds",
        budget_value=options.max_rounds,
    )
