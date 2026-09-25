"""Typed attempt and review facts shared by the hypothesis round-record builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, assert_never

from vibesys.evaluators.gates import FrameworkBenchmarkOutcome

if TYPE_CHECKING:
    from vibesys.schemas import (
        CandidateDisposition,
        HypothesisOutcome,
    )
    from vibesys.search.hypothesis.state import HypothesisState
    from vs_loop_state.api import JudgeVerdict, PerfProvenance


class CandidateReply(Protocol):
    """The candidate-checkpoint fields shared by both attempt reply shapes.

    Structural, not nominal: search must never import ``vibesys.roles``
    (roles depends on search, not the reverse), so this describes the shared
    shape of ``roles.implementer.ImplementerResponse`` and
    ``roles.single_agent.SingleAgentRoundResponse`` without importing either.
    """

    candidate_disposition: CandidateDisposition
    candidate_metrics: dict[str, float]
    candidate_evaluation_artifact: str | None
    candidate_operating_point: str
    candidate_retention_reason: str

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        """Serialize the reply, matching ``pydantic.BaseModel.model_dump``."""
        ...


class ImplementerReply(CandidateReply, Protocol):
    """The ``roles.implementer.ImplementerResponse`` fields attempt policies read."""

    hypothesis_outcome: HypothesisOutcome
    next_step: str
    perf_metric: float | None
    perf_unit: str | None
    metrics: dict[str, float]
    evaluation_artifact: str | None
    validation_recipe_artifact: str | None


class SingleAgentReply(CandidateReply, Protocol):
    """The ``roles.single_agent.SingleAgentRoundResponse`` fields attempt policies read."""

    perf_metric: float | None
    perf_unit: str | None
    profile_analysis: str
    bottlenecks: str
    suggestions: str


class JudgeSkipReason(StrEnum):
    """Why one implementer attempt received no independent judge verdict."""

    NOT_REACHED = "not_reached"
    UNPARSEABLE_IMPLEMENTATION = "unparseable_implementation"
    SPARSE_REVIEW_POLICY = "sparse_review_policy"


@dataclass(frozen=True, slots=True)
class JudgeReviewed:
    """An independent judge audited this attempt and returned a verdict.

    ``verdict`` carries the persisted pass/fail vocabulary
    (``vs_loop_state.JudgeVerdict``, minus its ``"deferred"`` member), not
    ``vibesys.roles.common.Verdict``: search must never import roles. A
    caller in ``loops/`` translates a role reply's ``Verdict`` to this string
    at the turn boundary.
    """

    verdict: Literal["pass", "fail"]


@dataclass(frozen=True, slots=True)
class JudgeSkipped:
    """No judge ran for this attempt, for the given reason."""

    reason: JudgeSkipReason


type JudgeOutcome = JudgeReviewed | JudgeSkipped


def attempt_was_reviewed(outcome: JudgeOutcome) -> bool:
    """Return whether an independent judge ruled on this attempt."""
    match outcome:
        case JudgeReviewed():
            return True
        case JudgeSkipped():
            return False
        case _:
            assert_never(outcome)


def recorded_judge_verdict(outcome: JudgeOutcome) -> JudgeVerdict:
    """Persist a skipped review as deferred, never as a prior verdict."""
    match outcome:
        case JudgeReviewed(verdict=verdict):
            return verdict
        case JudgeSkipped():
            return "deferred"
        case _:
            assert_never(outcome)


class AttemptDecision(StrEnum):
    """What the shared retry executor should do after a policy turn."""

    RETRY = "retry"
    FINISH = "finish"
    OFFICIAL = "official"


@dataclass
class AttemptState:
    """Mutable facts that survive retries within one framework round."""

    agent_run_state: HypothesisState
    feedback: str | None
    implementation: ImplementerReply | None = None
    single_agent_response: SingleAgentReply | None = None
    judge: JudgeOutcome = field(default_factory=lambda: JudgeSkipped(JudgeSkipReason.NOT_REACHED))
    passed: bool = False
    review_started: bool = False
    revalidation_required: bool = False
    official_reason: str | None = None
    framework_benchmark: FrameworkBenchmarkOutcome = field(
        default_factory=FrameworkBenchmarkOutcome
    )
    framework_perf_metric: float | None = None
    retry: int = 0


@dataclass(frozen=True)
class PerformanceProjection:
    """Performance evidence produced by an attempt policy for the round record."""

    metric: float | None
    unit: str | None
    provenance: PerfProvenance | None
    profile_skipped: bool
    accepted_metrics: dict[str, float]
    accepted_evaluation_artifact: str | None
    next_single_response: SingleAgentReply | None
