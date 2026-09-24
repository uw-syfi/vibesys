"""Agent orchestration's public projection and compatibility helpers.

Generic run/session contracts live in :mod:`vibesys.api`. Import this module
when an application explicitly handles the built-in agent policy.
"""

from __future__ import annotations

from vibesys.api._agent_state import agent_run_objectives, is_agent_run_manifest
from vibesys.api._orchestrations.agent_projection import (
    AgentRunProjection,
    HypothesisRoundView,
    HypothesisView,
    RoundView,
    agent_projection,
)
from vibesys.loops.agent.issue_board import framework_memory_paths

__all__ = [
    "AgentRunProjection",
    "HypothesisRoundView",
    "HypothesisView",
    "RoundView",
    "agent_projection",
    "agent_run_objectives",
    "framework_memory_paths",
    "is_agent_run_manifest",
]
