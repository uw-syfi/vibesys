"""Evolutionary-search plugin declaration."""

import json

from pydantic import BaseModel

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.orchestration.evolve.agents import AGENTS
from vibesys.orchestration.evolve.models import EvolveOptions, EvolveState
from vibesys.orchestration.evolve.orchestration import orchestrate
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import OrchestrationPlugin, OrchestrationResumeDecision, PluginProjection


def _project(raw_state: BaseModel) -> PluginProjection:
    state = EvolveState.model_validate(raw_state)
    return PluginProjection(
        payload={
            "population": state.population.model_dump(mode="json"),
            "metric_space": state.metric_space.model_dump(mode="json"),
            "generation": state.population.generation,
        }
    )


def _compare_resume(
    recorded: OrchestrationDescriptor,
    requested: OrchestrationDescriptor,
) -> OrchestrationResumeDecision:
    """Keep evolve policy fixed while allowing its generation budget to grow."""
    if (
        recorded.id != "evolve"
        or requested.id != "evolve"
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
                    f"resuming (recorded {old.max_generations}, "
                    f"requested {new.max_generations})"
                ),
            )
        )
    if new.max_generations == old.max_generations:
        return OrchestrationResumeDecision(descriptor=None)
    return OrchestrationResumeDecision(descriptor=requested, requires_clean_workspace=True)


def _project_max_rounds(options: BaseModel) -> int:
    return EvolveOptions.model_validate(options).max_generations


PLUGIN = OrchestrationPlugin(
    id="evolve",
    agents=AGENTS,
    options=EvolveOptions,
    state=EvolveState,
    orchestrate=orchestrate,
    project=_project,
    resume_policy=_compare_resume,
    project_max_rounds=_project_max_rounds,
)

__all__ = ["PLUGIN"]
