"""Public projection contract for built-in hypothesis-search plugins."""

from vibesys.orchestration.hypothesis.readmodel import (
    AgentRunProjection,
    HypothesisRoundView,
    HypothesisView,
    RoundView,
    agent_projection,
)
from vs_loop_state.api import (
    CandidateDisposition,
    HypothesisOutcome,
    HypothesisResolution,
    JudgeVerdict,
    PerfDeltaReason,
)

__all__ = [
    "AgentRunProjection",
    "CandidateDisposition",
    "HypothesisOutcome",
    "HypothesisResolution",
    "HypothesisRoundView",
    "HypothesisView",
    "JudgeVerdict",
    "PerfDeltaReason",
    "RoundView",
    "agent_projection",
]
