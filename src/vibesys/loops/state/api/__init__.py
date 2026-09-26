"""Public persisted loop-state records and pure serialization codecs.

The records cover plain, agent, and evolve loops. Parsers validate persisted
data, and serializers produce the corresponding state payloads. Filesystem
persistence is owned by ``vs_project.api``.
"""

from vibesys.evaluators.metrics import MetricComparison
from vibesys.loops.state.agent import (
    JudgeVerdict,
    PerfProvenance,
    RoundHistory,
    RoundRecord,
    parse_round_record,
    serialize_round_record,
)
from vibesys.loops.state.evolve import (
    IndividualRecord,
    PopulationSnapshot,
    parse_population_snapshot,
    serialize_population_snapshot,
)
from vibesys.loops.state.plain import (
    PlainLoopCursor,
    PlainPerformanceRecord,
    PlainPerformanceSnapshot,
    parse_plain_loop_cursor,
    parse_plain_performance_snapshot,
    serialize_plain_loop_cursor,
    serialize_plain_performance_snapshot,
)
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    HypothesisResolution,
    PerfDeltaReason,
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
