"""Explicit declaration of the first single-agent orchestration slice."""

from vibesys.orchestrations.single.agents import AGENTS
from vibesys.orchestrations.single.models import SingleOptions
from vibesys.orchestrations.single.orchestration import orchestrate
from vs_runtime.api import OrchestrationPlugin

PLUGIN = OrchestrationPlugin(
    id="single-agent",
    agents=AGENTS,
    options=SingleOptions,
    orchestrate=orchestrate,
)

__all__ = ["PLUGIN"]
