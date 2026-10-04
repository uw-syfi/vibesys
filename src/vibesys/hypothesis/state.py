"""Persisted state types for the hypothesis search, and their durable storage."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from vibesys.hypothesis.history import (
    CandidateDisposition,
    HypothesisOutcome,
    HypothesisResolution,
    PerfDeltaReason,
    PerfProvenance,
    RoundRecord,
)
from vibesys.hypothesis.plan import OrchestratorPlan
from vibesys.metrics import MetricSpace
from vibesys.profile_focus.state import ProfileFocusState

__all__ = [
    "Hypothesis",
    "HypothesisMeasurement",
    "HypothesisResolution",
    "HypothesisReview",
    "HypothesisState",
    "HypothesisStrategy",
    "InputBaseline",
    "PerfProvenance",
    "RoundRecord",
]


class HypothesisReview(StrEnum):
    """Independent review state, separate from empirical resolution."""

    PENDING = "pending"
    PASS = "pass"  # noqa: S105  # LW-010202 [S105]; this is the public judge verdict value, not a credential.
    FAIL = "fail"
    DEFERRED = "deferred"


class HypothesisStrategy(StrEnum):
    """Orchestrator-owned strategic treatment of a research direction."""

    AVAILABLE = "available"
    PARKED = "parked"
    ABANDONED = "abandoned"


def _validate_round_contract(record: RoundRecord) -> None:
    if record.judge_verdict is None:
        message = "round records must carry a judge_verdict"
        raise ValueError(message)
    if record.hypothesis_declared_outcome is not None:
        HypothesisOutcome(record.hypothesis_declared_outcome)
    if record.hypothesis_outcome is not None:
        try:
            HypothesisOutcome(record.hypothesis_outcome)
        except ValueError:
            HypothesisResolution(record.hypothesis_outcome)
    CandidateDisposition(record.candidate_disposition)
    if record.perf_metric is not None and record.perf_provenance is None:
        message = "round records with a headline metric must carry perf_provenance"
        raise ValueError(message)
    if (
        record.official_evaluation
        and record.perf_metric is not None
        and record.perf_provenance == "framework"
        and record.perf_comparison is None
    ):
        message = "trusted official round records must carry perf_comparison"
        raise ValueError(message)


class HypothesisMeasurement(BaseModel):
    """Official headline measurement and its causal comparison baseline."""

    model_config = ConfigDict(extra="forbid", strict=True)

    round: Annotated[int, Field(gt=0)]
    metric: str = Field(min_length=1)
    value: FiniteFloat
    unit: str | None = None
    direction: Literal["max", "min"] | None = None
    baseline_round: Annotated[int, Field(gt=0)] | None = None
    baseline_commit: str | None = None
    baseline_value: FiniteFloat | None = None
    delta_pct: FiniteFloat | None = None
    # Why ``delta_pct`` is None, when the evidence can say. None whenever a
    # baseline was found.
    delta_reason: PerfDeltaReason | None = None


class InputBaseline(BaseModel):
    """Framework benchmark of the run's input tree, taken once before round 1.

    It roots every baseline chain: a round with no retained, measured ancestor
    is compared against it, and a candidate is retained only if it also beats
    it. ``metric`` is the headline axis; ``metrics`` is the full measured row.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    commit: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    direction: Literal["max", "min"] | None = None
    metrics: dict[str, FiniteFloat]

    @model_validator(mode="after")
    def _headline_in_row(self) -> Self:
        if self.metric not in self.metrics:
            message = f"input baseline row must carry its headline metric {self.metric!r}"
            raise ValueError(message)
        return self

    def value(self, metric: str | None) -> float | None:
        """Return the measured value on *metric*, if this row carries it."""
        return self.metrics.get(metric) if metric is not None else None


class Hypothesis(BaseModel):
    """One hypothesis, including its plan, round evidence, and restart state."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    hypothesis_id: str = Field(min_length=1)
    plan: OrchestratorPlan
    started_round: Annotated[int, Field(gt=0)]
    parent_round: Annotated[int, Field(gt=0)] | None = None
    parent_commit: str | None = None
    rounds: list[RoundRecord] = Field(default_factory=list)

    feedback: str | None = None
    next_step: str | None = None
    continuation_rounds: Annotated[int, Field(ge=0)] = 0
    revert_applied: bool = False
    revert_commit: str | None = None
    gate_revalidation_pending: bool = False
    gate_approved_perf_metric: FiniteFloat | None = None
    gate_approved_perf_unit: str | None = None
    gate_approved_metrics: dict[str, FiniteFloat] = Field(default_factory=dict)
    gate_approved_evaluation_artifact: str | None = None
    gate_approved_candidate_disposition: str = CandidateDisposition.UNASSESSED.value
    gate_approved_candidate_metrics: dict[str, FiniteFloat] = Field(default_factory=dict)
    gate_approved_candidate_evaluation_artifact: str | None = None
    gate_approved_candidate_operating_point: str = ""
    gate_approved_candidate_retention_reason: str = ""
    gate_candidate_commit: str | None = None
    gate_accuracy_passed: bool = False

    declared_outcome: HypothesisOutcome | None = None
    review: HypothesisReview = HypothesisReview.PENDING
    resolution: HypothesisResolution | None = None
    measurement: HypothesisMeasurement | None = None
    candidate_retained: bool | None = None
    strategy: HypothesisStrategy = HypothesisStrategy.AVAILABLE
    strategy_reason: str | None = None
    # Revision of the last change visible through the experiment-log projection.
    # Restart-only checkpoint fields may change without advancing this value.
    last_experiment_revision: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _valid_identity(self) -> Self:
        if self.plan.hypothesis_id != self.hypothesis_id:
            message = "plan hypothesis_id must match its owning hypothesis"
            raise ValueError(message)
        round_numbers = [record.round_number for record in self.rounds]
        if round_numbers != sorted(set(round_numbers)):
            message = "hypothesis rounds must be unique and ordered"
            raise ValueError(message)
        if any(record.hypothesis_id != self.hypothesis_id for record in self.rounds):
            message = "round hypothesis_id must match its owning hypothesis"
            raise ValueError(message)
        for record in self.rounds:
            _validate_round_contract(record)
        return self

    def clone(self) -> Hypothesis:
        """Return an independent copy for computing the next state."""
        return self.model_copy(deep=True)


class HypothesisState(BaseModel):
    """The single authoritative state aggregate for an agent-loop run.

    ``metrics`` is the run's metric space: the objective axes and the
    measurement tolerance, written once when the run starts from the task's
    ``objectives.toml``. Every consumer that has to order two readings -- the
    loop, the hypothesis projection, checkpoint retention, the Pareto frontier,
    and the server read path -- reads it from here rather than being handed a
    tolerance through a call chain. An empty space is valid for runs without
    configured objective axes.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[1] = 1
    # Monotonic version of the experiment-log projection. The persisted
    # aggregate owns it; event and query cursors only report this value.
    experiment_revision: Annotated[int, Field(ge=0)] = 0
    active_hypothesis_id: str | None = None
    metrics: MetricSpace = Field(default_factory=MetricSpace)
    # ``None`` until the input tree is measured, and for runs without a
    # framework benchmark or whose input produced no headline metric. Omitted
    # from the serialized state while unset, so runs without one persist the
    # same document as before the field existed.
    input_baseline: InputBaseline | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    profile_guidance: ProfileFocusState | None = None

    @model_validator(mode="after")
    def _valid_identity(self) -> Self:
        identifiers = [item.hypothesis_id for item in self.hypotheses]
        if len(set(identifiers)) != len(identifiers):
            message = "hypothesis IDs must be unique"
            raise ValueError(message)
        if self.active_hypothesis_id is not None:
            active = self.by_id(self.active_hypothesis_id)
            if active is None:
                message = "active_hypothesis_id must name a known hypothesis"
                raise ValueError(message)
            if active.strategy is not HypothesisStrategy.AVAILABLE:
                message = "the active hypothesis must be strategically available"
                raise ValueError(message)
        round_numbers = [
            record.round_number for hypothesis in self.hypotheses for record in hypothesis.rounds
        ]
        if len(set(round_numbers)) != len(round_numbers):
            message = "round numbers must be globally unique"
            raise ValueError(message)
        return self

    def by_id(self, hypothesis_id: str) -> Hypothesis | None:
        """Return a detached hypothesis copy by stable ID."""
        hypothesis = next(
            (item for item in self.hypotheses if item.hypothesis_id == hypothesis_id),
            None,
        )
        return hypothesis.model_copy(deep=True) if hypothesis is not None else None

    @property
    def active_hypothesis(self) -> Hypothesis | None:
        """Return a detached copy of the active hypothesis, if any."""
        if self.active_hypothesis_id is None:
            return None
        return self.by_id(self.active_hypothesis_id)

    @property
    def rounds(self) -> list[RoundRecord]:
        """Return completed rounds in global chronological order."""
        return sorted(
            (record for hypothesis in self.hypotheses for record in hypothesis.rounds),
            key=lambda record: record.round_number,
        )

    def clone(self) -> HypothesisState:
        """Return an independent copy for computing the next state."""
        return self.model_copy(deep=True)
