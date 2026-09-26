"""Explicit declaration of the plain multi-agent orchestration."""

from vibesys.orchestrations.multi.agents import AGENTS
from vibesys.orchestrations.multi.models import MultiOptions, MultiState
from vibesys.orchestrations.multi.orchestration import orchestrate
from vs_runtime.api import OrchestrationPlugin

PLUGIN = OrchestrationPlugin(
    id="multi-agent",
    agents=AGENTS,
    options=MultiOptions,
    state=MultiState,
    orchestrate=orchestrate,
)

__all__ = ["PLUGIN"]
