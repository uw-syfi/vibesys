"""Permanent read-only field schemas of legacy dynamic state versions 1 through 9.

Frozen from the last legacy writer (parent of b7f0efda6f, #1784). Only field
schemas and closed enums survive here. No transition, coroutine position or
snapshot upgrade is used. Historical spellings are declared as fields so unknown
keys still fail. The importer extracts facts and discards all scheduling fields.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from vibesys.hypothesis import HypothesisOutcome
from vibesys.hypothesis import OrchestratorPlan as CurrentPlan
from vibesys.hypothesis.state import Hypothesis as CurrentHypothesis
from vibesys.hypothesis.state import HypothesisState as CurrentHypothesisState
from vs_runtime.api import AgentId, MetricDirection, PartialMeasurement, ProfileField

EvaluationStage = Literal["queued", "framework_setup", "accuracy", "benchmark", "profile"]
NonnegativeSeconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
EvaluationEvidenceId = str
MAX_PROFILE_QUESTION_CHARS = 4000


class LegacyPlan(CurrentPlan):
    minimum_acceptance_criteria: str = ""
    request_official_evaluation: bool = False
    revert_to_round: int | None = None


class LegacyHypothesis(CurrentHypothesis):
    plan: LegacyPlan


class HypothesisState(CurrentHypothesisState):
    hypotheses: list[LegacyHypothesis] = Field(default_factory=list)


class CandidateProfileStatus(StrEnum):
    OBSERVED = "observed"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class CandidateProfileComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    share: FiniteFloat = Field(ge=0, le=1)


class CandidateProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: str = Field(min_length=1)
    status: CandidateProfileStatus
    operation_id: str | None = Field(default=None, min_length=1)
    capture_started: bool | None = None
    diagnosis: str | None = None
    missing_fields: tuple[ProfileField, ...] = ()
    components: tuple[CandidateProfileComponent, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    failure: str | None = Field(default=None, min_length=1)


class IntentKind(StrEnum):
    PARK = "park"
    CANCEL = "cancel"
    INTERRUPT = "interrupt"
    TURN = "turn"
    REOPEN = "reopen"
    OBSERVE = "observe_evaluations"
    RESUME = "resume_agent_turn"
    INSPECT_EVALUATION = "inspect_evaluation"
    CANCEL_EVALUATION = "cancel_evaluation"


class IntentStage(StrEnum):
    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class EvaluationOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class EvaluationDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    handle: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    generation: Annotated[int, Field(ge=0)]
    candidate_revision: str = Field(min_length=1)
    candidate_digest: str = Field(pattern="^[0-9a-f]{64}$")
    evaluator_digest: str = Field(pattern="^[0-9a-f]{64}$")
    workload_digest: str = Field(pattern="^[0-9a-f]{64}$")
    environment_digest: str = Field(pattern="^[0-9a-f]{64}$")


class ProfilerDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["profiler"] = "profiler"
    handle: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    generation: Annotated[int, Field(ge=0)]
    candidate_revision: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    request_digest: str = Field(pattern="^[0-9a-f]{64}$")


class ContinuationStatus(StrEnum):
    ACTIVE = "active"
    PARKED = "parked"
    CANCELLED = "cancelled"


class EvaluationProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    observation_state: Literal["pending", "running", "unknown"] = "unknown"
    observed_at_s: NonnegativeSeconds | None = None
    stage: EvaluationStage | None = None
    queued_seconds: NonnegativeSeconds | None = None
    ran_seconds: NonnegativeSeconds | None = None
    pending_reason: str | None = None
    estimated_start_s: NonnegativeSeconds | None = None


class EvaluationTimeout(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    handle: str = Field(min_length=1)
    stage: EvaluationStage | None = None
    queued_seconds: NonnegativeSeconds | None = None
    ran_seconds: NonnegativeSeconds | None = None


class TimedOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["timed_out"] = "timed_out"
    deadline_at_s: NonnegativeSeconds
    reached_at_s: NonnegativeSeconds
    evaluations: tuple[EvaluationTimeout, ...] = Field(min_length=1)


class DependencyContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    continuation_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    generation: Annotated[int, Field(ge=1)]
    role: Literal["implementer", "judge"]
    session_key: str = Field(min_length=1)
    yielded_invocation_id: str = Field(min_length=1)
    retained_revision: str = Field(min_length=1)
    original_stage: Literal["implementing", "implemented"]
    evaluation_scope_id: str = Field(min_length=1)
    evaluation_generation: Annotated[int, Field(ge=0)]
    dependencies: tuple[EvaluationDependency | ProfilerDependency, ...] = Field(min_length=1)
    deadline_at_s: NonnegativeSeconds | None
    timed_out: TimedOut | None = None
    progress: dict[str, EvaluationProgress] = Field(default_factory=dict)
    settlements: dict[str, EvaluationOutcome] = Field(default_factory=dict)
    evidence_ids: dict[str, tuple[EvaluationEvidenceId, ...]] = Field(default_factory=dict)
    status: ContinuationStatus = ContinuationStatus.ACTIVE
    park_operation_id: str | None = Field(default=None, min_length=1)


EvaluationContinuation = DependencyContinuation


class LifecycleIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    generation: Annotated[int, Field(ge=1)]
    kind: IntentKind
    stage: IntentStage = IntentStage.PREPARED
    resume_revision: str | None = Field(default=None, min_length=1)
    invocation_id: str | None = Field(default=None, min_length=1)
    continuation_id: str | None = Field(default=None, min_length=1)
    evaluation_index: Annotated[int, Field(ge=0)] | None = None
    terminal_failure: Literal["evaluation_resume"] | None = None


class LifecycleState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intents: dict[str, LifecycleIntent] = Field(default_factory=dict)
    continuations: dict[str, EvaluationContinuation] = Field(default_factory=dict)
    stopped: bool = False


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    location: str = Field(min_length=1, max_length=512)
    purpose: str = Field(min_length=1, max_length=1000)
    revision: str | None = Field(default=None, min_length=1, max_length=256)


class WorkstreamKind(StrEnum):
    IMPLEMENT = "implement"
    PROFILE = "profile"


class WorkstreamPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal[WorkstreamKind.IMPLEMENT] = Field(
        default=WorkstreamKind.IMPLEMENT,
        description="Use implement when the slot must change code or produce a candidate.",
    )
    hypothesis_id: AgentId = Field(description="The hypothesis this workstream implements.")
    title: str = Field(min_length=1, description="Name of the implementation goal.")
    hypothesis: str = Field(min_length=1, description="The claim this implementation will test.")
    task: str = Field(min_length=1, description="The concrete changes the implementer must make.")
    pass_criteria: str = Field(
        min_length=1, description="Observable evidence that the implementation meets its goal."
    )
    continue_hypothesis: bool = False
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=8)
    parent_hypothesis_id: AgentId | None = Field(
        default=None,
        description="For a new hypothesis only: the ID of an existing hypothesis listed as a buildable candidate; the workstream starts from that candidate's revision. Null to start from the current base revision.",
    )
    request_evaluation: bool | None = None
    parent_revision: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        min_length=1,
        description="Exact offered revision of parent_hypothesis_id for a new sibling. Omitting it retains the legacy latest candidate selection.",
    )


class ProfilePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal[WorkstreamKind.PROFILE] = Field(
        description="Use profile only to measure an existing revision. It cannot implement a feature, fix correctness, change code, or produce a candidate."
    )
    profile_id: AgentId = Field(
        description="A new ID for this profile, distinct from every hypothesis and profile ID."
    )
    target_hypothesis_id: AgentId | None = Field(
        description="The ID of a hypothesis listed as a buildable candidate, to profile its revision; null to profile the current base revision."
    )
    question: str = Field(
        min_length=1,
        max_length=MAX_PROFILE_QUESTION_CHARS,
        description="What to measure on this revision, not an implementation task or kind choice.",
    )
    required_fields: tuple[ProfileField, ...] = Field(
        default=(),
        description="Required measurement fields. Aggregate timing cannot answer phase or HIP API requirements.",
    )
    decision_impact: str | None = Field(
        default=None,
        description="Which next implementation decision this measurement will inform, and how.",
    )


PlannedWorkstream = Annotated[WorkstreamPlan | ProfilePlan, Field(discriminator="kind")]


class StrategyReason(StrEnum):
    INFEASIBLE = "infeasible"
    FALSIFIED = "falsified"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"
    LOWER_PRIORITY = "lower_priority"


class MeasuredIteration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    hypothesis_id: AgentId | None
    sequence: int = Field(ge=0)
    revision: str
    name: str
    value: FiniteFloat
    direction: MetricDirection
    unit: str | None = None
    target: FiniteFloat | None = None


class ImplementerResult(BaseModel):
    validation_recipe_artifact: str | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)
    summary: str = Field(min_length=1)
    outcome: HypothesisOutcome
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=8)
    next_step: str = ""


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    passed: bool
    analysis: str = Field(min_length=1)
    feedback: str = ""


class EvaluationResult(BaseModel):
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
    partial_measurement: PartialMeasurement | None = None


class VerifiedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: str = Field(min_length=1)
    content_digest: str = Field(pattern="^[0-9a-f]{64}$")
    observation_sequence: int | None = Field(
        default=None, gt=0, exclude_if=lambda value: value is None
    )
    benchmark_passed: bool | None = None
    metric_name: str | None = None
    metric_value: FiniteFloat | None = None
    metric_unit: str | None = None
    metric_direction: MetricDirection | None = None
    partial_measurement: PartialMeasurement | None = None


class WorkstreamPhase(StrEnum):
    PENDING = "pending"
    IMPLEMENTING = "implementing"
    IMPLEMENTED = "implemented"
    REVIEWED = "reviewed"
    EVALUATED = "evaluated"
    FAILED = "failed"
    PARKED = "parked"
    CANCELLED = "cancelled"


class WorkstreamBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    spent: Annotated[int, Field(ge=0)] = 0
    refunded: Annotated[int, Field(ge=0)] = 0


class DynamicWorkstream(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    hypothesis_id: AgentId
    sequence: Annotated[int, Field(gt=0)]
    planning_call: Annotated[int, Field(gt=0)] | None = None
    plan: WorkstreamPlan
    parent_revision: str
    epoch: int | None = None
    attempts: int | None = None
    refunded_attempts: int | None = None
    member_id: str | None = None
    evaluation_eligibility_counted: bool | None = None
    cadence_evaluation_due: bool | None = None
    phase: WorkstreamPhase = WorkstreamPhase.PENDING
    budget: WorkstreamBudget = Field(default_factory=WorkstreamBudget)
    invocation_sequence: Annotated[int, Field(ge=0)] = 0
    candidate_revision: str | None = None
    implementation: ImplementerResult | None = None
    review: ReviewResult | None = None
    evaluation: EvaluationResult | None = None
    prior_attempt: str = ""
    measured_iterations: tuple[MeasuredIteration, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    lineage_parent_id: AgentId | None = Field(default=None, exclude_if=lambda value: value is None)
    strategy_reason_kind: StrategyReason | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    prior_revision: str | None = None
    feedback: str | None = None
    last_error: str | None = None
    setup_failure: bool = False
    implementer_started: bool = False
    verified: VerifiedCandidate | None = None


class DynamicProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: AgentId
    sequence: Annotated[int, Field(gt=0)]
    planning_call: Annotated[int, Field(gt=0)] | None = None
    plan: ProfilePlan
    revision: str = Field(min_length=1)
    outcome: CandidateProfile | None = None


class InputMeasurementAttempts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: str = Field(min_length=1)
    attempts: Annotated[int, Field(ge=0)] = 0


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    milestone: str
    expected_minutes: Annotated[float, Field(ge=2, le=240)]
    reason: str


class QueuedStart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    spec: PlannedWorkstream
    expectation: Expectation
    priority: Literal["now", "next", "later"] = "now"


class SteerNote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    note_sha256: str
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    sent_at_s: float
    interrupt: bool
    reserved_to: str | None = None
    delivered_to: str | None = None
    dropped: Literal["workstream_settled"] | None = None


class JournalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    at_s: float
    turn: int
    kind: Literal["start", "steer", "park", "cancel", "reflect", "turn_report", "finish"]
    subject: AgentId | None
    text: str


class AgentLoopState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    generation: Annotated[int, Field(ge=1)] = 1
    turns: Annotated[int, Field(ge=0)] = 0
    expectations: dict[AgentId, Expectation] = Field(default_factory=dict)
    queue: list[QueuedStart] = Field(default_factory=list)
    steers: dict[AgentId, list[SteerNote]] = Field(default_factory=dict)
    journal: list[JournalEntry] = Field(default_factory=list)
    next_check_in_s: float | None = None
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    capabilities_withdrawn: list[Literal["profile"]] = Field(default_factory=list)
    finished: str | None = None


class DynamicState(BaseModel):
    next_epoch: int | None = None
    eligible_evaluation_candidates: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9] = 1
    lifecycle: LifecycleState = Field(default_factory=LifecycleState)
    agent: AgentLoopState | None = None
    experiment_revision: Annotated[int, Field(ge=0)] = 0
    next_planning_call: Annotated[int, Field(gt=0)] | None = None
    search: HypothesisState = Field(default_factory=HypothesisState)
    workstreams: list[DynamicWorkstream] = Field(default_factory=list)
    profiles: list[DynamicProfile] = Field(default_factory=list)
    baseline: EvaluationResult | None = None
    input_measurement: InputMeasurementAttempts | None = None
    winner_revision: str | None = None
    adoption_pending: bool = False
