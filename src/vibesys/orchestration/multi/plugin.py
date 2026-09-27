"""Explicit declarations of the multi-agent orchestration presets."""

from pydantic import BaseModel

from vibesys.orchestration.hypothesis_readmodel import project_hypothesis_state
from vibesys.orchestration.multi.agents import AGENTS
from vibesys.orchestration.multi.models import MultiOptions, MultiState, ProfileGuidedMultiOptions
from vibesys.orchestration.multi.orchestration import orchestrate, orchestrate_profile_guided
from vs_runtime.api import OrchestrationPlugin, PluginProjection


def _project(raw_state: BaseModel) -> PluginProjection:
    """Project the multi-agent aggregate into its public policy view."""
    return project_hypothesis_state(MultiState.model_validate(raw_state).search)


PLUGIN = OrchestrationPlugin(
    id="multi-agent",
    agents=AGENTS,
    options=MultiOptions,
    state=MultiState,
    orchestrate=orchestrate,
    project=_project,
)

PROFILE_GUIDED_PLUGIN = OrchestrationPlugin(
    id="profile-guided-multi-agent",
    agents=AGENTS,
    options=ProfileGuidedMultiOptions,
    state=MultiState,
    orchestrate=orchestrate_profile_guided,
    project=_project,
)

__all__ = ["PLUGIN", "PROFILE_GUIDED_PLUGIN"]
