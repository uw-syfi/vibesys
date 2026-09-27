"""Pure hypothesis-lifecycle search policy.

Deterministic logic for the hypothesis-driven strategies (multi, single,
profile_multi, profile_single): persisted state, round-record construction,
attempt/review bookkeeping, and the state-transition functions those compose.
See :class:`~vibesys.orchestration.hypothesis.search.HypothesisSearch` for the
public entry point.
"""

from __future__ import annotations

from vibesys.orchestration.hypothesis.attempts import (
    AttemptDecision,
    AttemptState,
    CandidateReply,
    ImplementerReply,
    JudgeOutcome,
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    PerformanceProjection,
    SingleAgentReply,
    attempt_was_reviewed,
    recorded_judge_verdict,
)
from vibesys.orchestration.hypothesis.config import HypothesisConfig
from vibesys.orchestration.hypothesis.plan import (
    HypothesisStrategyUpdate,
    OrchestratorPlan,
)
from vibesys.orchestration.hypothesis.record import (
    CandidateEvidence,
    MeasurementEvidence,
    RecordInput,
    build_round_record,
)
from vibesys.orchestration.hypothesis.results import (
    AttemptBudget,
    CarryOver,
    ClosedRound,
    Continue,
    Finished,
    NewHypothesis,
    NextRoundDecision,
    PlanningContext,
    RollbackTarget,
    StartedHypothesis,
)
from vibesys.orchestration.hypothesis.search import HypothesisSearch
from vibesys.orchestration.hypothesis.state import (
    Hypothesis,
    HypothesisMeasurement,
    HypothesisResolution,
    HypothesisReview,
    HypothesisState,
    HypothesisStrategy,
)

__all__ = [
    "AttemptBudget",
    "AttemptDecision",
    "AttemptState",
    "CandidateEvidence",
    "CandidateReply",
    "CarryOver",
    "ClosedRound",
    "Continue",
    "Finished",
    "Hypothesis",
    "HypothesisConfig",
    "HypothesisMeasurement",
    "HypothesisResolution",
    "HypothesisReview",
    "HypothesisSearch",
    "HypothesisState",
    "HypothesisStrategy",
    "HypothesisStrategyUpdate",
    "ImplementerReply",
    "JudgeOutcome",
    "JudgeReviewed",
    "JudgeSkipReason",
    "JudgeSkipped",
    "MeasurementEvidence",
    "NewHypothesis",
    "NextRoundDecision",
    "OrchestratorPlan",
    "PerformanceProjection",
    "PlanningContext",
    "RecordInput",
    "RollbackTarget",
    "SingleAgentReply",
    "StartedHypothesis",
    "attempt_was_reviewed",
    "build_round_record",
    "recorded_judge_verdict",
]
