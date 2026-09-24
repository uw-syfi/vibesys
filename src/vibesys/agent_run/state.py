"""Authoritative agent run state models and their durable storage."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from vibesys.evaluators.metrics import MetricSpace
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    OrchestratorPlan,
    PerfDeltaReason,
)
from vs_loop_state.api import HypothesisResolution, RoundRecord

if TYPE_CHECKING:
    from vs_project.api import Project, StateNamespace


class HypothesisReview(StrEnum):
    """Independent review state, separate from empirical resolution."""

    PENDING = "pending"
    PASS = "pass"  # noqa: S105  # tracked: #288
    FAIL = "fail"
    DEFERRED = "deferred"


class HypothesisStrategy(StrEnum):
    """Orchestrator-owned strategic treatment of a research direction."""

    AVAILABLE = "available"
    PARKED = "parked"
    ABANDONED = "abandoned"


class ProfileGuidanceStatus(StrEnum):
    """Framework-owned lifecycle state for one profiled component."""

    OPEN = "open"
    ACTIVE = "active"
    EXHAUSTED = "exhausted"


class ProfileBottleneck(BaseModel):
    """One ranked cost center from a task-owned profiler."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    cost: Annotated[FiniteFloat, Field(ge=0)]
    share: Annotated[FiniteFloat, Field(ge=0, le=1)]
    evidence: list[str] = Field(default_factory=list)


class ProfileAttributionSample(BaseModel):
    """A component's measured profile share in one planning round."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round: Annotated[int, Field(gt=0)]
    cost: Annotated[FiniteFloat, Field(ge=0)]
    share: Annotated[FiniteFloat, Field(ge=0, le=1)]
    evidence: list[str] = Field(default_factory=list)


class ProfileImprovementSample(BaseModel):
    """A direction-normalized relative improvement in one completed round."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round: Annotated[int, Field(gt=0)]
    relative_improvement: FiniteFloat


class ProfileGuidedComponent(BaseModel):
    """Typed durable state for one component in a profile-guided walk."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(min_length=1)
    status: ProfileGuidanceStatus = ProfileGuidanceStatus.OPEN
    rounds_spent: Annotated[int, Field(ge=0)] = 0
    stalled_rounds: Annotated[int, Field(ge=0)] = 0
    latest_cost: Annotated[FiniteFloat, Field(ge=0)] | None = None
    latest_share: Annotated[FiniteFloat, Field(ge=0, le=1)] | None = None
    attribution_history: list[ProfileAttributionSample] = Field(default_factory=list)
    improvement_history: list[ProfileImprovementSample] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered_history(self) -> Self:
        for name, samples in (
            ("attribution", self.attribution_history),
            ("improvement", self.improvement_history),
        ):
            rounds = [sample.round for sample in samples]
            if rounds != sorted(set(rounds)):
                raise ValueError(  # noqa: TRY003
                    f"{name} history rounds must be unique and ordered"
                )
        return self


class ProfileGuidanceState(BaseModel):
    """Authoritative run-scoped cursor for profile-guided hypotheses."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    active_component: str | None = None
    components: list[ProfileGuidedComponent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_cursor(self) -> Self:
        names = [component.name for component in self.components]
        if len(names) != len(set(names)):
            raise ValueError("profile-guided component names must be unique")  # noqa: TRY003
        active = [
            component.name
            for component in self.components
            if component.status is ProfileGuidanceStatus.ACTIVE
        ]
        if active != ([self.active_component] if self.active_component is not None else []):
            raise ValueError(  # noqa: TRY003
                "active_component must name the only active component"
            )
        return self


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
    # baseline was found or the record predates provenance tracking.
    delta_reason: PerfDeltaReason | None = None


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
            raise ValueError(  # noqa: TRY003  # tracked: #288
                "plan hypothesis_id must match its owning hypothesis"
            )
        round_numbers = [record.round_number for record in self.rounds]
        if round_numbers != sorted(set(round_numbers)):
            raise ValueError(  # noqa: TRY003  # tracked: #288
                "hypothesis rounds must be unique and ordered"
            )
        if any(record.hypothesis_id != self.hypothesis_id for record in self.rounds):
            raise ValueError(  # noqa: TRY003  # tracked: #288
                "round hypothesis_id must match its owning hypothesis"
            )
        return self

    def clone(self) -> Hypothesis:
        """Return an independent copy for computing the next state."""
        return self.model_copy(deep=True)


class AgentRunState(BaseModel):
    """The single authoritative state aggregate for an agent-loop run.

    ``metrics`` is the run's metric space: the objective axes and the
    measurement tolerance, written once when the run starts from the task's
    ``objectives.toml``. Every consumer that has to order two readings -- the
    loop, the hypothesis projection, checkpoint retention, the Pareto frontier,
    and the server read path -- reads it from here rather than being handed a
    tolerance through a call chain. State written before this field existed
    loads as the empty strict space, which is the behavior those runs had.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[1] = 1
    # Monotonic version of the experiment-log projection. The persisted
    # aggregate owns it; event and query cursors only report this value.
    experiment_revision: Annotated[int, Field(ge=0)] = 0
    active_hypothesis_id: str | None = None
    metrics: MetricSpace = Field(default_factory=MetricSpace)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    profile_guidance: ProfileGuidanceState | None = None

    @model_validator(mode="after")
    def _valid_identity(self) -> Self:
        identifiers = [item.hypothesis_id for item in self.hypotheses]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("hypothesis IDs must be unique")  # noqa: TRY003  # tracked: #288
        if self.active_hypothesis_id is not None:
            active = self.by_id(self.active_hypothesis_id)
            if active is None:
                raise ValueError(  # noqa: TRY003  # tracked: #288
                    "active_hypothesis_id must name a known hypothesis"
                )
            if active.strategy is not HypothesisStrategy.AVAILABLE:
                raise ValueError(  # noqa: TRY003  # tracked: #288
                    "the active hypothesis must be strategically available"
                )
        round_numbers = [
            record.round_number for hypothesis in self.hypotheses for record in hypothesis.rounds
        ]
        if len(set(round_numbers)) != len(round_numbers):
            raise ValueError("round numbers must be globally unique")  # noqa: TRY003  # tracked: #288
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

    def clone(self) -> AgentRunState:
        """Return an independent copy for computing the next state."""
        return self.model_copy(deep=True)


class AgentRunStateStore:
    """Persist agent policy state in the run's portable v4 namespace."""

    def __init__(self, namespace: StateNamespace) -> None:
        """Bind the single typed state slot in the supplied namespace."""
        self._namespace = namespace
        self._slot = namespace.slot("state.json", AgentRunState)

    def load_optional(self) -> AgentRunState | None:
        """Return the authoritative aggregate when present."""
        return self._slot.load_optional()

    def load(self) -> AgentRunState:
        """Return the aggregate or a new empty state."""
        return self.load_optional() or AgentRunState()

    @property
    def namespace(self) -> StateNamespace:
        """Return the namespace used for Git snapshots."""
        return self._namespace


def load_agent_run_state(project: Project, run_id: str, *, namespace: str) -> AgentRunState | None:
    """Load one agent run's state without legacy format recovery."""
    return AgentRunStateStore(project.state.portable_namespace(run_id, namespace)).load_optional()
