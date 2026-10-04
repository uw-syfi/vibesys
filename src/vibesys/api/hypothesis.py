"""Public projection contract for built-in hypothesis-search plugins."""

from vibesys.hypothesis import (
    CandidateDisposition,
    HypothesisOutcome,
    HypothesisResolution,
    JudgeVerdict,
    PerfDeltaReason,
    PerfProvenance,
    RoundHistory,
    RoundRecord,
    parse_round_record,
    serialize_round_record,
)
from vibesys.hypothesis.readmodel import (
    AgentRunProjection,
    HypothesisRoundView,
    HypothesisView,
    RoundView,
    agent_projection,
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
    "PerfProvenance",
    "RoundHistory",
    "RoundRecord",
    "RoundView",
    "agent_projection",
    "parse_round_record",
    "serialize_round_record",
]
