"""`MultiStrategyState`: only the scientific state of a multi-agent search.

Lifecycle and accounting facts (attempt phases, slot use, charges, session phases, the
evidence ledger, settlements) are read from `RunView`, never copied. What lives here is policy
memory core cannot know: the hypothesis ledger (two JSON strings the round machine decodes),
the progress of the run-scoped turns that design the next hypothesis, the one open round, the
input baseline reading and the chosen winner.

Every model is a strict, frozen, tuple-only `Value`, so the state round-trips through the
envelope codec (`validate_immutable_schema`) unchanged.
"""

from enum import StrEnum

from pydantic import Field

from vibesys.candidates.api import BaselineState, RunPhase, Winner
from vibesys.metrics import AcceptedReading, MetricRow
from vibesys.orchestration.multi.strategy._prompts import (
    ArchiveConflictView,
    PromptContext,
    ValidationFailure,
)
from vibesys.serial_search.api import LastProfile
from vs_core.api import (
    AttemptTrack,
    DecisionId,
    EvidenceKey,
    InvocationRef,
    RevisionRef,
    SchemaRef,
    SettlementId,
    Step,
    StrategyState,
    TurnChain,
    Value,
)

STATE_SCHEMA = SchemaRef(name="multi.strategy-state", version=1)

# Advisory failures the state keeps for an operator to read.
NOTE_LIMIT = 20


class Role(StrEnum):
    """Agent roles; values match the `multi-<role>` role ids."""

    PLANNER = "orchestrator"
    PROFILER = "profiler"
    IMPLEMENTER = "implementer"
    JUDGE = "judge"


class Track(StrEnum):
    """The run-scoped steps that precede a new hypothesis's attempt, in order."""

    CAPTURE = "capture"
    PRE_ROUND = "pre-round"
    PROFILE = "profile"
    PLAN = "plan"


class WorkPhase(StrEnum):
    """Scientific phase of the open round; core owns the attempt lifecycle."""

    START = "start"
    IMPLEMENT = "implement"
    REVIEW = "review"
    VALIDATE = "validate"
    MEASURE = "measure"
    INTERPRET = "interpret"
    SETTLE = "settle"
    DONE = "done"


class UnreachableReason(StrEnum):
    """Typed reason a decision the strategy wants cannot be proposed yet."""

    RESUME_CAPABILITY_NOT_OFFERED = "resume-capability-not-offered"
    VALIDATE_LOCAL_NOT_OFFERED = "validate-local-not-offered"


class Unreachable(Value):
    """A decision kept unproposed, with the missing core contract named."""

    reason: UnreachableReason
    subject: str


class TurnRecord(TurnChain[PromptContext]):
    """One role's turn chain: free or paid, corrections and resumes."""

    role: Role


class DesignState(Value):
    """Progress of the run-scoped steps that design the next new hypothesis."""

    active: bool = False
    track: Track = Track.CAPTURE
    step: Step = Step.NEEDED
    awaiting: DecisionId | None = None
    turn: TurnRecord | None = None
    # The next unused serial of the run-scoped turns.
    serial: int = Field(default=0, ge=0)
    # What the pending correction tells the designer was wrong with its plan.
    last_error: str | None = None
    failed: bool = False
    # The routing decision: what to profile, or None when no profile is wanted.
    profile_focus: str | None = None
    # The fresh profile of this design, for the planning request.
    profile: LastProfile | None = None


class RoundOutcome(Value):
    """What core settled for one closed round, kept for winner choice."""

    round_number: int = Field(gt=0)
    hypothesis_id: str
    settlement: SettlementId
    candidate: RevisionRef | None = None
    eligible: bool = False
    metrics: tuple[MetricRow, ...] = ()


class RoundState(AttemptTrack[TurnRecord]):
    """Scientific progress of the open round (one attempt)."""

    number: int = Field(gt=0)
    hypothesis_id: str = Field(min_length=1)
    plan_json: str
    base: RevisionRef
    phase: WorkPhase = WorkPhase.START
    candidate: RevisionRef | None = None
    # The last valid implementer reply (JSON).
    response_json: str | None = None
    feedback: str | None = None
    validation_failure: ValidationFailure | None = None
    no_valid_reply: str | None = None
    # The live-archive conflict that turned a passing review into a failing one.
    conflict: ArchiveConflictView | None = None
    # A review has started in this round, so it must finish; a gate failure reopens review.
    review_started: bool = False
    review_skipped: bool = False
    revalidation_required: bool = False
    judge_pass: bool | None = None
    judge_invocation: InvocationRef | None = None
    validated: bool = False
    official_reason: str | None = None
    planned_official_reason: str | None = None
    evidence: tuple[EvidenceKey, ...] = ()
    readings: tuple[AcceptedReading, ...] = ()
    passed: bool = False
    gate_failed: bool = False
    unmeasured: bool = False
    # Paid implementer turns charged so far, bounded by `max_retries_per_round`.
    turns_spent: int = Field(default=0, ge=0)
    measurements: int = Field(default=0, ge=0)
    next_serial: int = Field(default=0, ge=0)


class MultiStrategyState(StrategyState):
    """Persisted scientific state of the multi strategy."""

    schema_version: int = Field(default=STATE_SCHEMA.version, ge=1)
    phase: RunPhase = RunPhase.SEARCHING
    stopping: bool = False
    baseline: BaselineState = BaselineState()
    # The hypothesis ledger as the round machine (`vibesys.hypothesis.rounds`) stores it.
    search_json: str = ""
    carry_json: str = ""
    design: DesignState = DesignState()
    round: RoundState | None = None
    closed: int = Field(default=0, ge=0)
    outcomes: tuple[RoundOutcome, ...] = ()
    # The newest revision a later round may build on (a retained checkpoint).
    checkpoint: RevisionRef | None = None
    winner: Winner | None = None
    unreachable: tuple[Unreachable, ...] = ()
    notes: tuple[str, ...] = ()
