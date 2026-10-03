"""Strict contracts and durable state for dynamic hypothesis portfolios."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vibesys.orchestration.hypothesis.plan import HypothesisStrategyUpdate
from vibesys.orchestration.hypothesis.state import HypothesisState
from vs_loop_state.api import HypothesisOutcome
from vs_runtime.api import MetricDirection


class DynamicOptions(AgentOrchestrationOptions):
    """Validated policy controls for the dynamic orchestration."""

    max_in_flight: Annotated[int, Field(gt=0, le=32)] = 2

    @model_validator(mode="after")
    def _supported_interface(self) -> DynamicOptions:
        if self.interface not in {"inprocess", "service"}:
            message = f"unsupported dynamic interface {self.interface!r}"
            raise ValueError(message)
        return self


class EvidenceReference(BaseModel):
    """Compact pointer to evidence stored outside agent conversation history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    location: str = Field(min_length=1, max_length=512)
    purpose: str = Field(min_length=1, max_length=256)
    revision: str | None = Field(default=None, min_length=1, max_length=256)


class WorkstreamPlan(BaseModel):
    """One causally independent hypothesis selected for parallel work."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=80)
    hypothesis: str = Field(min_length=1, max_length=2000)
    task: str = Field(min_length=1, max_length=4000)
    pass_criteria: str = Field(min_length=1, max_length=2000)
    continue_hypothesis: bool = False
    # No effect: every review-passed nominated or supported candidate gets a
    # trusted evaluation. Kept so existing plans and planner replies validate.
    request_evaluation: bool = False
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=8)


class PortfolioPlan(BaseModel):
    """A bounded batch of distinct hypothesis workstreams."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(min_length=1, max_length=2000)
    workstreams: tuple[WorkstreamPlan, ...] = Field(min_length=1, max_length=32)
    hypothesis_updates: tuple[HypothesisStrategyUpdate, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def _distinct_hypotheses(self) -> PortfolioPlan:
        identifiers = [item.hypothesis_id for item in self.workstreams]
        if len(identifiers) != len(set(identifiers)):
            message = "portfolio workstreams must use distinct hypothesis IDs"
            raise ValueError(message)
        return self


class ImplementerResult(BaseModel):
    """An implementer's compact, evidence-linked result."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2000)
    outcome: HypothesisOutcome
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=8)
    next_step: str = Field(default="", max_length=1000)
    validation_recipe_artifact: str | None = Field(default=None, min_length=1, max_length=512)


class ReviewResult(BaseModel):
    """Independent decision over one candidate and its linked evidence."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    analysis: str = Field(min_length=1, max_length=2000)
    feedback: str = Field(default="", max_length=1000)


class EvaluationResult(BaseModel):
    """Trusted evaluation facts recorded against an exact candidate revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str
    local_validation_passed: bool | None = None
    local_validation_feedback: str | None = None
    accuracy_passed: bool | None = None
    accuracy_feedback: str | None = None
    benchmark_passed: bool | None = None
    benchmark_feedback: str | None = None
    metric_name: str | None = None
    metric_value: FiniteFloat | None = None
    metric_direction: MetricDirection | None = None
    metric_unit: str | None = None
    metrics: dict[str, FiniteFloat] = Field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        """Return whether every configured gate represented here passed."""
        local = self.local_validation_passed is not False
        accuracy = self.accuracy_passed is not False
        benchmark = self.benchmark_passed is not False
        return local and accuracy and benchmark


class WorkstreamPhase(StrEnum):
    """Recoverable progress states for one isolated candidate."""

    PENDING = "pending"
    IMPLEMENTING = "implementing"
    IMPLEMENTED = "implemented"
    REVIEWED = "reviewed"
    EVALUATED = "evaluated"
    FAILED = "failed"


class DynamicWorkstream(BaseModel):
    """Durable lifecycle for one stable hypothesis identity."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    member_id: str
    sequence: Annotated[int, Field(gt=0)]
    epoch: Annotated[int, Field(gt=0)]
    plan: WorkstreamPlan
    parent_revision: str
    phase: WorkstreamPhase = WorkstreamPhase.PENDING
    attempts: Annotated[int, Field(ge=0)] = 0
    candidate_revision: str | None = None
    implementation: ImplementerResult | None = None
    review: ReviewResult | None = None
    evaluation: EvaluationResult | None = None
    # Retired evaluation-cadence bookkeeping, kept so older state loads.
    evaluation_eligibility_counted: bool = False
    cadence_evaluation_due: bool = False
    # Interrupted implementation attempts that resume did not count against
    # the retry budget; bounded so a repeatedly crashing attempt ends.
    refunded_attempts: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _stable_identity(self) -> DynamicWorkstream:
        if self.member_id != self.hypothesis_id:
            message = "workstream member_id must equal its stable hypothesis_id"
            raise ValueError(message)
        if self.plan.hypothesis_id != self.hypothesis_id:
            message = "workstream plan must preserve its hypothesis_id"
            raise ValueError(message)
        implemented = self.phase in {
            WorkstreamPhase.IMPLEMENTED,
            WorkstreamPhase.REVIEWED,
            WorkstreamPhase.EVALUATED,
        }
        if implemented and (self.candidate_revision is None or self.implementation is None):
            message = f"{self.phase.value} workstream requires a retained implementation"
            raise ValueError(message)
        if (
            self.phase in {WorkstreamPhase.REVIEWED, WorkstreamPhase.EVALUATED}
            and self.review is None
        ):
            message = f"{self.phase.value} workstream requires a review"
            raise ValueError(message)
        if self.phase is WorkstreamPhase.EVALUATED and self.evaluation is None:
            message = "evaluated workstream requires trusted evaluation evidence"
            raise ValueError(message)
        return self


class DynamicState(BaseModel):
    """The dynamic plugin's complete durable aggregate."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[1] = 1
    experiment_revision: Annotated[int, Field(ge=0)] = 0
    next_epoch: Annotated[int, Field(gt=0)] = 1
    search: HypothesisState = Field(default_factory=HypothesisState)
    workstreams: list[DynamicWorkstream] = Field(default_factory=list)
    # Retired evaluation-cadence counter, kept so older state loads.
    eligible_evaluation_candidates: Annotated[int, Field(ge=0)] = 0
    # The input (root) revision's trusted benchmark, measured once per run.
    # Candidates must beat it to be adopted or built on.
    baseline: EvaluationResult | None = None
    winner_revision: str | None = None
    adoption_pending: bool = False

    @model_validator(mode="after")
    def _valid_history(self) -> DynamicState:
        identifiers = [item.hypothesis_id for item in self.workstreams]
        if len(identifiers) != len(set(identifiers)):
            message = "dynamic state contains duplicate hypothesis IDs"
            raise ValueError(message)
        return self


__all__ = [
    "DynamicOptions",
    "DynamicState",
    "DynamicWorkstream",
    "EvaluationResult",
    "EvidenceReference",
    "ImplementerResult",
    "PortfolioPlan",
    "ReviewResult",
    "WorkstreamPhase",
    "WorkstreamPlan",
]
