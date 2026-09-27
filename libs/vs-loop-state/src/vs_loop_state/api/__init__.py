"""Public hypothesis-search records and pure serialization codecs."""

from vs_loop_state.agent import (
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
from vs_loop_state.metrics import MetricComparison

__all__ = [
    "CandidateDisposition",
    "HypothesisOutcome",
    "HypothesisResolution",
    "JudgeVerdict",
    "MetricComparison",
    "PerfDeltaReason",
    "PerfProvenance",
    "RoundHistory",
    "RoundRecord",
    "parse_round_record",
    "serialize_round_record",
]
