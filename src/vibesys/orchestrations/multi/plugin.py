"""Explicit declaration of the plain multi-agent orchestration."""

from vibesys.orchestrations.multi.agents import AGENTS
from vibesys.orchestrations.multi.models import MultiOptions, MultiState, ProfileGuidedMultiOptions
from vibesys.orchestrations.multi.orchestration import orchestrate, orchestrate_profile_guided
from vs_runtime.api import OrchestrationPlugin

PLUGIN = OrchestrationPlugin(
    id="multi-agent",
    agents=AGENTS,
    options=MultiOptions,
    state=MultiState,
    orchestrate=orchestrate,
)

PROFILE_GUIDED_PLUGIN = OrchestrationPlugin(
    id="profile-guided-multi-agent",
    agents=AGENTS,
    options=ProfileGuidedMultiOptions,
    state=MultiState,
    orchestrate=orchestrate_profile_guided,
)

__all__ = ["PLUGIN", "PROFILE_GUIDED_PLUGIN"]
