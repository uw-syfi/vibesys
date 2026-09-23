"""Persisted configuration and resume policy for the agent orchestrations.

Version 4 stores this policy as an opaque descriptor. ``vs_project`` only
validates its JSON shape; these settings and their meaning belong here.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.orchestration import OrchestrationResumeDecision
from vs_project.api import (
    AgentRunConfiguration,
    OrchestrationDescriptor,
    OrchestrationRunManifest,
)

AGENT_CONFIG_VERSION = 1
AGENT_ORCHESTRATION_IDS = frozenset({"agent", "profile-guided"})


class UnsupportedAgentOrchestrationError(ValueError):
    """A run descriptor names an unknown agent policy or version."""

    def __init__(self, orchestration_id: str, version: int) -> None:
        """Name the descriptor identity and unsupported config version."""
        super().__init__(
            f"Unsupported agent orchestration {orchestration_id!r} configuration version {version}"
        )


class AgentOrchestrationOptions(BaseModel):
    """Strict version 1 agent policy options, excluding shared run environment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    inner_loop: str
    interface: str
    model: str | None = None
    agent_backend: str
    agent_driver: str | None = None
    cli_provider: str | None = None
    cli_timeout: Annotated[int, Field(gt=0)] | None = None
    compute_backend: str
    profiler: str | None = None
    modality: str | None = None
    default_reasoning_effort: str | None = None
    outer_model: str | None = None
    outer_reasoning_effort: str | None = None
    inner_model: str | None = None
    inner_reasoning_effort: str | None = None
    max_rounds: Annotated[int, Field(gt=0)]
    max_retries_per_round: Annotated[int, Field(gt=0)]
    judge_every: Annotated[int, Field(gt=0)]
    official_eval_every: Annotated[int, Field(gt=0)]
    memory_layout: str
    operator_constraints: tuple[str, ...] = ()
    objectives: tuple[str, ...] = ()


def descriptor_from_configuration(configuration: AgentRunConfiguration) -> OrchestrationDescriptor:
    """Build a versioned descriptor from the selected agent run policy."""
    options = AgentOrchestrationOptions.model_validate(
        configuration.model_dump(exclude={"outer_loop", "run_environment"})
    )
    return OrchestrationDescriptor(
        id=configuration.outer_loop,
        config_version=AGENT_CONFIG_VERSION,
        options=options.model_dump(mode="json"),
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


def configuration_from_manifest(manifest: OrchestrationRunManifest) -> AgentRunConfiguration:
    """Project v4 agent options into the existing CLI configuration contract.

    This adapter is temporary while CLI restoration accepts the v3 shape.
    Agent policy validation happens in this module before that projection.
    """
    options = options_from_descriptor(manifest.orchestration)
    payload = options.model_dump(mode="json")
    payload["outer_loop"] = manifest.orchestration.id
    payload["run_environment"] = manifest.run_environment.model_dump(mode="json")
    return AgentRunConfiguration.model_validate_json(json.dumps(payload))
