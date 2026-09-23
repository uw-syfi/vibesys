"""Public persisted loop-state records and pure serialization codecs.

The records cover plain, agent, and evolve loops. Parsers validate persisted
data, and serializers produce the corresponding state payloads. Filesystem
persistence is owned by ``vs_project.api``.
"""

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
from vs_loop_state.evolve import (
    IndividualRecord,
    PopulationSnapshot,
    parse_population_snapshot,
    serialize_population_snapshot,
)
from vs_loop_state.metrics import MetricComparison
from vs_loop_state.plain import (
    PlainLoopCursor,
    PlainPerformanceRecord,
    PlainPerformanceSnapshot,
    parse_plain_loop_cursor,
    parse_plain_performance_snapshot,
    serialize_plain_loop_cursor,
    serialize_plain_performance_snapshot,
)

__all__ = [
    "CandidateDisposition",
    "HypothesisOutcome",
    "HypothesisResolution",
    "IndividualRecord",
    "JudgeVerdict",
    "MetricComparison",
    "PerfDeltaReason",
    "PerfProvenance",
    "PlainLoopCursor",
    "PlainPerformanceRecord",
    "PlainPerformanceSnapshot",
    "PopulationSnapshot",
    "RoundHistory",
    "RoundRecord",
    "parse_plain_loop_cursor",
    "parse_plain_performance_snapshot",
    "parse_population_snapshot",
    "parse_round_record",
    "serialize_plain_loop_cursor",
    "serialize_plain_performance_snapshot",
    "serialize_population_snapshot",
    "serialize_round_record",
]
