"""Typed attempt and review facts shared by the hypothesis round-record builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, assert_never

from vibesys.metrics import FrameworkBenchmarkOutcome

if TYPE_CHECKING:
    from vibesys.hypothesis.history import (
        CandidateDisposition,
        HypothesisOutcome,
        JudgeVerdict,
        PerfProvenance,
    )
    from vibesys.hypothesis.state import HypothesisState


class CandidateReply(Protocol):
    """The candidate-checkpoint fields shared by both attempt reply shapes.

    Structural, not nominal: search must never import an orchestration policy
    (policies depend on search, not the reverse), so this describes the shared
    shape of a policy-owned implementer response and
    ``orchestration.single.models.SingleAgentRoundResponse`` without importing it.
    Every member is read-only: a reply is a frozen value.
    """

    @property
    def candidate_disposition(self) -> CandidateDisposition:
        """The reply's candidate disposition."""
        ...

    @property
    def candidate_metrics(self) -> dict[str, float]:
        """The reply's candidate metrics."""
        ...

    @property
    def candidate_evaluation_artifact(self) -> str | None:
        """The reply's candidate evaluation artifact."""
        ...

    @property
    def candidate_operating_point(self) -> str:
        """The reply's candidate operating point."""
        ...

    @property
    def candidate_retention_reason(self) -> str:
        """The reply's candidate retention reason."""
        ...

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        """Serialize the reply, matching ``pydantic.BaseModel.model_dump``."""
        ...


class ImplementerReply(CandidateReply, Protocol):
    """The multi implementer-response fields attempt policies read."""

    @property
    def hypothesis_outcome(self) -> HypothesisOutcome:
        """The reply's hypothesis outcome."""
        ...

    @property
    def next_step(self) -> str:
        """The reply's next step."""
        ...

    @property
    def perf_metric(self) -> float | None:
        """The reply's perf metric."""
        ...

    @property
    def perf_unit(self) -> str | None:
        """The reply's perf unit."""
        ...

    @property
    def metrics(self) -> dict[str, float]:
        """The reply's metrics."""
        ...

    @property
    def evaluation_artifact(self) -> str | None:
        """The reply's evaluation artifact."""
        ...

    @property
    def validation_recipe_artifact(self) -> str | None:
        """The reply's validation recipe artifact."""
        ...


class SingleAgentReply(CandidateReply, Protocol):
    """The single plugin response fields attempt policies read."""

    @property
    def perf_metric(self) -> float | None:
        """The reply's perf metric."""
        ...

    @property
    def perf_unit(self) -> str | None:
        """The reply's perf unit."""
        ...

    @property
    def profile_analysis(self) -> str:
        """The reply's profile analysis."""
        ...

    @property
    def bottlenecks(self) -> str:
        """The reply's bottlenecks."""
        ...

    @property
    def suggestions(self) -> str:
        """The reply's suggestions."""
        ...


class JudgeSkipReason(StrEnum):
    """Why one implementer attempt received no independent judge verdict."""

    NOT_REACHED = "not_reached"
    UNPARSEABLE_IMPLEMENTATION = "unparseable_implementation"
    SPARSE_REVIEW_POLICY = "sparse_review_policy"


@dataclass(frozen=True, slots=True)
class JudgeReviewed:
    """An independent judge audited this attempt and returned a verdict.

    ``verdict`` carries the persisted pass/fail vocabulary
    (``vibesys.hypothesis.history.JudgeVerdict``, minus its ``"deferred"`` member), not
    ``vibesys.orchestration.review.Verdict``. A policy caller translates its reply verdict to this string
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
    evaluated_only: bool = False
    """The trusted gates measured a revision the agent evaluated although the reply offered no
    ready candidate (blocked, failed, continuing, or no reply): the measurement is recorded as
    evidence, and the revision is never retained for it."""
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
