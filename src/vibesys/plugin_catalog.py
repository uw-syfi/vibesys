"""Product composition for the built-in orchestration plugins.

Plugin declarations contain only app-developer policy. This module privately
adapts the product lifecycle facts still required by the current VibeSys host.
Delete these ``RunSetup`` factories when those facts move behind ``vs_runtime``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibesys.context import RunSetup, RunStartHints
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.orchestration import OrchestrationResumeDecision
from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vibesys.orchestration.contracts import OrchestrationRegistry
from vibesys.orchestration.evolve.models import EvolveOptions
from vibesys.orchestration.evolve.plugin import PLUGIN as EVOLVE_PLUGIN
from vibesys.orchestration.issue_queue.models import IssueQueueOptions
from vibesys.orchestration.issue_queue.plugin import (
    PLUGIN as ISSUE_QUEUE_PLUGIN,
)
from vibesys.orchestration.memory import declared_memory_paths
from vibesys.orchestration.multi.models import MultiOptions, ProfileGuidedMultiOptions
from vibesys.orchestration.multi.plugin import (
    PLUGIN as MULTI_PLUGIN,
)
from vibesys.orchestration.multi.plugin import (
    PROFILE_GUIDED_PLUGIN as PROFILE_MULTI_PLUGIN,
)
from vibesys.orchestration.single.models import ProfileGuidedSingleOptions, SingleOptions
from vibesys.orchestration.single.plugin import (
    PLUGIN as SINGLE_PLUGIN,
)
from vibesys.orchestration.single.plugin import (
    PROFILE_GUIDED_PLUGIN as PROFILE_SINGLE_PLUGIN,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vs_project.api import OrchestrationDescriptor


def _compare_resume(
    recorded: OrchestrationDescriptor,
    requested: OrchestrationDescriptor,
    *,
    orchestration_id: str,
    options_type: type[AgentOrchestrationOptions] | type[IssueQueueOptions],
) -> OrchestrationResumeDecision:
    """Adapt strict plugin options to the legacy host's resume contract."""
    if (
        recorded.id != orchestration_id
        or requested.id != orchestration_id
        or recorded.config_version != 1
        or requested.config_version != 1
    ):
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message="resuming a run cannot change its orchestration ID or config version",
            )
        )
    old = options_type.model_validate_json(json.dumps(recorded.options), strict=True)
    new = options_type.model_validate_json(json.dumps(requested.options), strict=True)
    changed = tuple(
        name
        for name in options_type.model_fields
        if name != "max_rounds" and getattr(old, name) != getattr(new, name)
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
    old_rounds = int(old.max_rounds)
    new_rounds = int(new.max_rounds)
    if new_rounds < old_rounds:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=(
                    "max_rounds is the run's total limit and cannot decrease when resuming "
                    f"(recorded {old_rounds}, requested {new_rounds})"
                ),
            )
        )
    if new_rounds == old_rounds:
        return OrchestrationResumeDecision(descriptor=None)
    return OrchestrationResumeDecision(descriptor=requested, requires_clean_workspace=True)


def _compare_single_resume(
    recorded: OrchestrationDescriptor, requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    return _compare_resume(
        recorded,
        requested,
        orchestration_id=SINGLE_PLUGIN.id,
        options_type=SingleOptions,
    )


def _compare_profile_single_resume(
    recorded: OrchestrationDescriptor, requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    return _compare_resume(
        recorded,
        requested,
        orchestration_id=PROFILE_SINGLE_PLUGIN.id,
        options_type=ProfileGuidedSingleOptions,
    )


def _compare_multi_resume(
    recorded: OrchestrationDescriptor, requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    return _compare_resume(
        recorded,
        requested,
        orchestration_id=MULTI_PLUGIN.id,
        options_type=MultiOptions,
    )


def _compare_profile_multi_resume(
    recorded: OrchestrationDescriptor, requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    return _compare_resume(
        recorded,
        requested,
        orchestration_id=PROFILE_MULTI_PLUGIN.id,
        options_type=ProfileGuidedMultiOptions,
    )


def _compare_issue_queue_resume(
    recorded: OrchestrationDescriptor, requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    return _compare_resume(
        recorded,
        requested,
        orchestration_id=ISSUE_QUEUE_PLUGIN.id,
        options_type=IssueQueueOptions,
    )


def _compare_evolve_resume(
    recorded: OrchestrationDescriptor, requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    """Keep evolve policy fixed while allowing its total generation budget to grow."""
    if (
        recorded.id != EVOLVE_PLUGIN.id
        or requested.id != EVOLVE_PLUGIN.id
        or recorded.config_version != 1
        or requested.config_version != 1
    ):
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message="resuming a run cannot change its orchestration ID or config version",
            )
        )
    old = EvolveOptions.model_validate_json(json.dumps(recorded.options), strict=True)
    new = EvolveOptions.model_validate_json(json.dumps(requested.options), strict=True)
    changed = tuple(
        name
        for name in EvolveOptions.model_fields
        if name != "max_generations" and getattr(old, name) != getattr(new, name)
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
                    f"resuming (recorded {old.max_generations}, requested {new.max_generations})"
                ),
            )
        )
    if new.max_generations == old.max_generations:
        return OrchestrationResumeDecision(descriptor=None)
    return OrchestrationResumeDecision(descriptor=requested, requires_clean_workspace=True)


def _hypothesis_setup(
    options: BaseModel,
    *,
    resume_policy: Callable[
        [OrchestrationDescriptor, OrchestrationDescriptor], OrchestrationResumeDecision
    ],
) -> RunSetup:
    parsed = AgentOrchestrationOptions.model_validate(options)
    return RunSetup(
        resume_policy=resume_policy,
        start_hints=RunStartHints(max_rounds=parsed.max_rounds),
        memory_paths=declared_memory_paths(),
    )


def _single_setup(options: BaseModel) -> RunSetup:
    return _hypothesis_setup(options, resume_policy=_compare_single_resume)


def _profile_single_setup(options: BaseModel) -> RunSetup:
    return _hypothesis_setup(options, resume_policy=_compare_profile_single_resume)


def _multi_setup(options: BaseModel) -> RunSetup:
    return _hypothesis_setup(options, resume_policy=_compare_multi_resume)


def _profile_multi_setup(options: BaseModel) -> RunSetup:
    return _hypothesis_setup(options, resume_policy=_compare_profile_multi_resume)


def _issue_queue_setup(options: BaseModel) -> RunSetup:
    parsed = IssueQueueOptions.model_validate(options)
    return RunSetup(
        resume_policy=_compare_issue_queue_resume,
        start_hints=RunStartHints(max_rounds=parsed.max_rounds),
    )


def _evolve_setup(options: BaseModel) -> RunSetup:
    parsed = EvolveOptions.model_validate(options)
    return RunSetup(
        resume_policy=_compare_evolve_resume,
        start_hints=RunStartHints(max_rounds=parsed.max_generations),
    )


def built_in_orchestrations() -> OrchestrationRegistry:
    """Compose in-repository policy plugins with the VibeSys product host."""
    registry = OrchestrationRegistry()
    registry.register_plugin(SINGLE_PLUGIN, setup=_single_setup, state_family="agent")
    registry.register_plugin(
        PROFILE_SINGLE_PLUGIN,
        setup=_profile_single_setup,
        state_family="agent",
    )
    registry.register_plugin(MULTI_PLUGIN, setup=_multi_setup, state_family="agent")
    registry.register_plugin(
        PROFILE_MULTI_PLUGIN,
        setup=_profile_multi_setup,
        state_family="agent",
    )
    registry.register_plugin(ISSUE_QUEUE_PLUGIN, setup=_issue_queue_setup)
    registry.register_plugin(EVOLVE_PLUGIN, setup=_evolve_setup)
    return registry


__all__ = ["built_in_orchestrations"]
