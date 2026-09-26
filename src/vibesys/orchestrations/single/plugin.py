"""Explicit declarations of the single-agent orchestration presets."""

from vibesys.orchestrations.single.agents import AGENTS
from vibesys.orchestrations.single.models import (
    ProfileGuidedSingleOptions,
    SingleOptions,
    SingleState,
)
from vibesys.orchestrations.single.orchestration import orchestrate, orchestrate_profile_guided
from vs_runtime.api import OrchestrationPlugin

PLUGIN = OrchestrationPlugin(
    id="single-agent",
    agents=AGENTS,
    options=SingleOptions,
    state=SingleState,
    orchestrate=orchestrate,
)

PROFILE_GUIDED_PLUGIN = OrchestrationPlugin(
    id="profile-guided-single-agent",
    agents=AGENTS,
    options=ProfileGuidedSingleOptions,
    state=SingleState,
    orchestrate=orchestrate_profile_guided,
)

__all__ = ["PLUGIN", "PROFILE_GUIDED_PLUGIN"]
