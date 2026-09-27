"""Explicit declarations of the single-agent orchestration presets."""

from functools import partial

from pydantic import BaseModel

from vibesys.orchestration.hypothesis.readmodel import project_hypothesis_state
from vibesys.orchestration.memory import declared_memory_paths
from vibesys.orchestration.resume import compare_round_budget, project_round_budget
from vibesys.orchestration.single.agents import AGENTS
from vibesys.orchestration.single.models import (
    ProfileGuidedSingleOptions,
    SingleOptions,
    SingleState,
)
from vibesys.orchestration.single.orchestration import orchestrate, orchestrate_profile_guided
from vs_runtime.api import OrchestrationPlugin, PluginProjection


def _project(raw_state: BaseModel) -> PluginProjection:
    """Project the single-agent aggregate into its public policy view."""
    return project_hypothesis_state(SingleState.model_validate(raw_state).search)


PLUGIN = OrchestrationPlugin(
    id="single-agent",
    agents=AGENTS,
    options=SingleOptions,
    state=SingleState,
    orchestrate=orchestrate,
    project=_project,
    resume_policy=partial(
        compare_round_budget,
        plugin_id="single-agent",
        options_type=SingleOptions,
    ),
    memory_paths=declared_memory_paths(),
    project_max_rounds=partial(project_round_budget, options_type=SingleOptions),
)

PROFILE_GUIDED_PLUGIN = OrchestrationPlugin(
    id="profile-guided-single-agent",
    agents=AGENTS,
    options=ProfileGuidedSingleOptions,
    state=SingleState,
    orchestrate=orchestrate_profile_guided,
    project=_project,
    resume_policy=partial(
        compare_round_budget,
        plugin_id="profile-guided-single-agent",
        options_type=ProfileGuidedSingleOptions,
    ),
    memory_paths=declared_memory_paths(),
    project_max_rounds=partial(
        project_round_budget,
        options_type=ProfileGuidedSingleOptions,
    ),
)

__all__ = ["PLUGIN", "PROFILE_GUIDED_PLUGIN"]
