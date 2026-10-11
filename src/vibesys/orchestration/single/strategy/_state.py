"""`SingleStrategyState`: only the scientific state of a single-agent search.

Lifecycle and accounting facts (attempt phases, slot use, charges, session phases, the
evidence ledger, settlements) are read from `RunView`, never copied. What lives here is policy
memory core cannot know: the hypothesis ledger (two JSON strings the round machine decodes), the
designer's turn progress, the one open round, the input baseline reading and the chosen winner.

Every model is a strict, frozen, tuple-only `Value`, so the state round-trips through the
envelope codec (`validate_immutable_schema`) unchanged.
"""

from enum import StrEnum

from pydantic import Field

from vibesys.candidates.api import BaselineState, RunPhase, Winner
from vibesys.metrics import AcceptedReading, MetricRow
from vibesys.orchestration.single.strategy._prompts import PromptContext
from vibesys.serial_search.api import LastProfile
from vs_core.api import (
    AttemptTrack,
    DecisionId,
    EvidenceKey,
    RevisionRef,
    SchemaRef,
    SettlementId,
    Step,
    StrategyState,
    TurnChain,
    Value,
)

STATE_SCHEMA = SchemaRef(name="single.strategy-state", version=1)


class Role(StrEnum):
    """Agent roles; values match the `single-<role>` role ids."""

    PLANNER = "orchestrator"
    IMPLEMENTER = "implementer"


class WorkPhase(StrEnum):
    """Scientific phase of the open round; core owns the attempt lifecycle."""

    START = "start"
    IMPLEMENT = "implement"
    MEASURE = "measure"
    INTERPRET = "interpret"
    SETTLE = "settle"
    DONE = "done"


class UnreachableReason(StrEnum):
    """Typed reason a decision the strategy wants cannot be proposed yet."""

    CAPTURE_OPERATION_NOT_OFFERED = "capture-operation-not-offered"
    RESUME_CAPABILITY_NOT_OFFERED = "resume-capability-not-offered"


class Unreachable(Value):
    """A decision kept unproposed, with the missing core contract named."""

    reason: UnreachableReason
    subject: str


class TurnRecord(TurnChain[PromptContext]):
    """One role's turn chain: free or paid, corrections and resumes."""

    role: Role


class PlannerState(Value):
    """Designer turn, correction and attribution progress for the next new hypothesis."""

    step: Step = Step.NEEDED
    active: bool = False
    awaiting: DecisionId | None = None
    turn: TurnRecord | None = None
    serial: int = Field(default=0, ge=0)
    last_error: str | None = None
    failed: bool = False
    # Profile-guided preset: the attribution capture precedes the designer's turn.
    capture_step: Step = Step.NEEDED
    capture_awaiting: DecisionId | None = None
    capture_done: bool = False


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
    # The last valid combined reply (JSON), after the Pareto guard.
    response_json: str | None = None
    feedback: str | None = None
    no_valid_reply: str | None = None
    dominated_by_rounds: tuple[int, ...] = ()
    verdict_pass: bool | None = None
    official_reason: str | None = None
    evidence: tuple[EvidenceKey, ...] = ()
    readings: tuple[AcceptedReading, ...] = ()
    passed: bool = False
    gate_failed: bool = False
    unmeasured: bool = False
    # Paid implementer turns charged so far, bounded by `max_retries_per_round`.
    turns_spent: int = Field(default=0, ge=0)
    measurements: int = Field(default=0, ge=0)
    next_serial: int = Field(default=0, ge=0)


class SingleStrategyState(StrategyState):
    """Persisted scientific state of the single strategy."""

    schema_version: int = Field(default=STATE_SCHEMA.version, ge=1)
    phase: RunPhase = RunPhase.SEARCHING
    stopping: bool = False
    baseline: BaselineState = BaselineState()
    # The hypothesis ledger as the round machine (`vibesys.hypothesis.rounds`) stores it.
    search_json: str = ""
    carry_json: str = ""
    planner: PlannerState = PlannerState()
    round: RoundState | None = None
    closed: int = Field(default=0, ge=0)
    outcomes: tuple[RoundOutcome, ...] = ()
    # The newest revision a later round may build on (a retained checkpoint).
    checkpoint: RevisionRef | None = None
    last_profile: LastProfile | None = None
    winner: Winner | None = None
    unreachable: tuple[Unreachable, ...] = ()
