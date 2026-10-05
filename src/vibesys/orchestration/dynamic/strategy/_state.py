"""`DynamicStrategyState`: only the scientific state of a dynamic search.

Lifecycle and accounting facts (attempt phases, slot occupancy, charges, session
phases, evidence ledger, settlements) are never copied here; they are derived
from `RunView`. What lives here is policy memory core cannot know: hypothesis
history, planner correction and refill progress, lineage and parent choices, the
input baseline reading and the chosen winner.

Every model is a strict, frozen, tuple-only `Value`, so the state round-trips
through the envelope codec (`validate_immutable_schema`) unchanged.
"""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from vibesys.hypothesis import HypothesisOutcome, HypothesisStrategy
from vibesys.orchestration.dynamic.strategy._parents import ParentSnapshot
from vibesys.orchestration.dynamic.strategy._prompts import PromptContext
from vibesys.orchestration.dynamic.strategy._rows import AcceptedReading, MetricRow, PartialRow
from vs_core.api import (
    ArtifactRef,
    AttemptId,
    ContinuationId,
    DecisionId,
    EvidenceKey,
    InvocationId,
    InvocationRef,
    RevisionRef,
    SchemaRef,
    SettlementId,
    StrategyState,
    Value,
)

STATE_SCHEMA = SchemaRef(name="dynamic.strategy-state", version=1)


class RunPhase(StrEnum):
    """Where the whole search is."""

    SEARCHING = "searching"
    SELECTING = "selecting"
    ADOPTING = "adopting"
    STOPPING = "stopping"
    FINISHED = "finished"


class Role(StrEnum):
    """Agent roles; values match the `dynamic-<role>` role ids."""

    PLANNER = "orchestrator"
    IMPLEMENTER = "implementer"
    JUDGE = "judge"
    PROFILER = "profiler"


class Step(StrEnum):
    """Progress of the current phase of one subject through propose, wait, settle.

    NEEDED: decide must propose the phase's next decision. RENDERING / RENDERED:
    a prompt render is in flight or done. AWAITING: a decision or turn is in
    flight and an event moves the subject on. SUSPENDED: the turn yielded to wait
    for its evaluations and only `ResumeAuthorized` moves it on. RETAINING: a write
    turn answered, but core has not yet retained (or declined) its checkpoint, so
    the reply is held until `decide` can read the candidate from the view.
    """

    NEEDED = "needed"
    RENDERING = "rendering"
    RENDERED = "rendered"
    AWAITING = "awaiting"
    SUSPENDED = "suspended"
    RETAINING = "retaining"


class WorkKind(StrEnum):
    """What a scheduled workstream does with its slot."""

    IMPLEMENT = "implement"
    PROFILE = "profile"


class WorkPhase(StrEnum):
    """Scientific phase of one workstream; core owns the attempt lifecycle."""

    VERIFY_PARENT = "verify_parent"
    START = "start"
    IMPLEMENT = "implement"
    PROFILE = "profile"
    REVIEW = "review"
    MEASURE = "measure"
    INTERPRET = "interpret"
    SETTLE = "settle"
    DONE = "done"


class UnreachableReason(StrEnum):
    """Typed reason a decision the strategy wants cannot be proposed yet."""

    RETAIN_OPERATION_NOT_OFFERED = "retain-operation-not-offered"
    PROFILE_CAPABILITY_NOT_OFFERED = "profile-capability-not-offered"
    RESUME_CAPABILITY_NOT_OFFERED = "resume-capability-not-offered"


class Unreachable(Value):
    """A decision kept unproposed, with the missing core contract named."""

    reason: UnreachableReason
    subject: str


class WorkPlan(Value):
    """The planner's validated choice for one workstream, in immutable form."""

    kind: WorkKind
    work_id: str = Field(min_length=1)
    title: str = ""
    hypothesis: str = ""
    task: str = ""
    pass_criteria: str = ""
    continue_hypothesis: bool = False
    evidence: tuple[str, ...] = ()
    parent_hypothesis_id: str | None = None
    parent_revision: str | None = None
    question: str = ""
    required_fields: tuple[str, ...] = ()
    decision_impact: str = ""


class TurnRecord(Value):
    """Bookkeeping of one role's turn chain: free, paid, corrections and resumes."""

    role: Role
    serial: int = Field(default=0, ge=0)
    corrections: int = Field(default=0, ge=0)
    charge: Literal["free", "paid", "correction", "resume"] = "paid"
    context: PromptContext | None = None
    prompts: tuple[ArtifactRef, ...] = ()
    tool_policy: ArtifactRef | None = None
    # Predecessor of a resumed turn: the suspended invocation core is resuming.
    invocation: InvocationRef | None = None
    # The invocation ID core authorized for a resume; None for every other turn.
    resume_as: InvocationId | None = None
    continuation: ContinuationId | None = None
    # The reply asked to wait for evaluations; core's `TurnSuspended` completes the yield.
    yielded: bool = False


class PlannerState(Value):
    """Planner turn, correction and refill progress."""

    call: int = Field(default=1, ge=1)
    step: Step = Step.NEEDED
    active: bool = False
    awaiting: DecisionId | None = None
    turn: TurnRecord | None = None
    # Finished workstreams when a call scheduled nothing. Planning resumes only after
    # another workstream finishes, so a planner that keeps failing cannot spin.
    blocked_at_done: int | None = Field(default=None, ge=0)
    capacity: int = Field(default=0, ge=0)
    # The first valid plan that left slots free, kept as the fallback if the
    # correction does not improve it (reply JSON, parsed on demand).
    held_plan_json: str | None = None
    last_error: str | None = None
    failed: bool = False


class BaselineStage(StrEnum):
    """Input measurement progress; the baseline gates candidates and adoption."""

    NEEDED = "needed"
    AWAITING = "awaiting"
    INTERPRET = "interpret"
    INTERPRETING = "interpreting"
    MEASURED = "measured"
    UNMEASURABLE = "unmeasurable"
    NOT_CONFIGURED = "not-configured"


class BaselineState(Value):
    """The input revision's reading, measured once per run."""

    stage: BaselineStage = BaselineStage.NEEDED
    attempts: int = Field(default=0, ge=0)
    awaiting: DecisionId | None = None
    evidence: tuple[EvidenceKey, ...] = ()
    accuracy_passed: bool | None = None
    benchmark_passed: bool | None = None
    metrics: tuple[MetricRow, ...] = ()
    partial: PartialRow | None = None
    failure: str | None = None


class RoundRecord(Value):
    """One finished workstream round of a hypothesis."""

    sequence: int = Field(gt=0)
    attempt: AttemptId
    outcome: HypothesisOutcome | None = None
    summary: str = ""
    review_passed: bool | None = None
    candidate: RevisionRef | None = None
    accuracy_passed: bool | None = None
    benchmark_passed: bool | None = None
    metrics: tuple[MetricRow, ...] = ()
    partial: PartialRow | None = None
    eligible: bool = False
    failure: str | None = None
    settlement: SettlementId | None = None
    # Whether the implementer asked to keep this direction and the lease allows it.
    kept_active: bool = False


class HypothesisRecord(Value):
    """A hypothesis's identity, lineage, strategy treatment and rounds."""

    hypothesis_id: str = Field(min_length=1)
    title: str
    hypothesis: str
    first_sequence: int = Field(gt=0)
    lineage_parent_id: str | None = None
    strategy: HypothesisStrategy = HypothesisStrategy.AVAILABLE
    reason_kind: str | None = None
    reason: str = ""
    last_task: str = ""
    continuation_rounds: int = Field(default=0, ge=0)
    rounds: tuple[RoundRecord, ...] = ()


class AttemptRecord(Value):
    """Scientific progress of one scheduled workstream."""

    plan: WorkPlan
    sequence: int = Field(gt=0)
    attempt: AttemptId
    generation: int = Field(default=0, ge=0)
    # The parent revision is frozen when the workstream is scheduled; a later edit
    # of its producer, or a better candidate, never changes it.
    parent: RevisionRef
    parent_hypothesis_id: str | None = None
    phase: WorkPhase
    step: Step = Step.NEEDED
    awaiting: DecisionId | None = None
    turn: TurnRecord | None = None
    candidate: RevisionRef | None = None
    outcome: HypothesisOutcome | None = None
    summary: str = ""
    next_step: str = ""
    review_passed: bool | None = None
    feedback: str | None = None
    evidence: tuple[EvidenceKey, ...] = ()
    readings: tuple[AcceptedReading, ...] = ()
    judge_invocation: InvocationRef | None = None
    failure: str | None = None
    withdrawn: bool = False
    # Core has admitted the attempt, so leaving it requires a settlement.
    ready: bool = False
    # Implementer turns charged so far, bounded by `max_retries_per_round`.
    turns_spent: int = Field(default=0, ge=0)
    # The serial the next first turn of a role takes: one past every serial used in this
    # attempt, so a retry's turn and a later review never reuse a turn's decision ID.
    next_serial: int = Field(default=0, ge=0)
    settle_sent: bool = False
    # The implementer reply (JSON) and turn waiting for its checkpoint, while RETAINING.
    held_reply: str | None = None
    held_invocation: InvocationRef | None = None


class Winner(Value):
    """The retained candidate proposed for adoption, or the trusted baseline."""

    settlement: SettlementId | None
    revision: RevisionRef
    hypothesis_id: str | None


class DynamicStrategyState(StrategyState):
    """Persisted scientific state of the dynamic strategy."""

    schema_version: int = Field(default=STATE_SCHEMA.version, ge=1)
    phase: RunPhase = RunPhase.SEARCHING
    stopping: bool = False
    baseline: BaselineState = BaselineState()
    planner: PlannerState = PlannerState()
    hypotheses: tuple[HypothesisRecord, ...] = ()
    attempts: tuple[AttemptRecord, ...] = ()
    parents: tuple[ParentSnapshot, ...] = ()
    winner: Winner | None = None
    # Workstreams refunded because a profile ended unsupported.
    refunded: int = Field(default=0, ge=0)
    # Parent revisions whose verification failed; never offered to the planner again.
    withheld: tuple[str, ...] = ()
    unreachable: tuple[Unreachable, ...] = ()
