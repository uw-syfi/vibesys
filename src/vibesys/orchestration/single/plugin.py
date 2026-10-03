"""Explicit declarations of the single-agent orchestration presets."""

from functools import partial

from pydantic import BaseModel

from vibesys.hypothesis.readmodel import project_hypothesis_state
from vibesys.orchestration.memory import declared_memory_paths
from vibesys.orchestration.resume import compare_round_budget, project_round_budget
from vibesys.orchestration.single.agents import AGENTS
from vibesys.orchestration.single.models import (
    ProfileGuidedSingleOptions,
    SingleOptions,
    SingleState,
)
from vibesys.orchestration.single.orchestration import orchestrate, orchestrate_profile_guided
from vibesys.plugin_registration import OrchestrationRegistration
from vibesys.run.contracts import PluginProjection
from vs_runtime.api import OrchestrationPlugin


def _project(raw_state: BaseModel) -> PluginProjection:
    """Project the single-agent aggregate into its public policy view."""
    return project_hypothesis_state(SingleState.model_validate(raw_state).search)


PLUGIN = OrchestrationPlugin(
    id="single-agent",
    agents=AGENTS,
    options=SingleOptions,
    state=SingleState,
    orchestrate=orchestrate,
    memory_paths=declared_memory_paths(),
)
REGISTRATION = OrchestrationRegistration(
    plugin=PLUGIN,
    project=_project,
    resume_policy=partial(
        compare_round_budget,
        plugin_id="single-agent",
        options_type=SingleOptions,
    ),
    project_max_rounds=partial(project_round_budget, options_type=SingleOptions),
)

PROFILE_GUIDED_PLUGIN = OrchestrationPlugin(
    id="profile-guided-single-agent",
    agents=AGENTS,
    options=ProfileGuidedSingleOptions,
    state=SingleState,
    orchestrate=orchestrate_profile_guided,
    memory_paths=declared_memory_paths(),
)
PROFILE_GUIDED_REGISTRATION = OrchestrationRegistration(
    plugin=PROFILE_GUIDED_PLUGIN,
    project=_project,
    resume_policy=partial(
        compare_round_budget,
        plugin_id="profile-guided-single-agent",
        options_type=ProfileGuidedSingleOptions,
    ),
    project_max_rounds=partial(
        project_round_budget,
        options_type=ProfileGuidedSingleOptions,
    ),
)

__all__ = [
    "PLUGIN",
    "PROFILE_GUIDED_PLUGIN",
    "PROFILE_GUIDED_REGISTRATION",
    "REGISTRATION",
]
