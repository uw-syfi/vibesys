"""The plain values the dynamic search keeps and passes around, with no behavior of their own.

Every value is a strict, frozen `Value` with closed fields. Reasons are enums with the typed
facts that go with them; the only free text is text an agent or an evaluator wrote (an
implementer's summary, a reviewer's feedback, what a failed check printed), carried as data for
a later prompt and never written by the search.
"""

from enum import StrEnum

from pydantic import Field, model_validator

from vibesys.hypothesis import HypothesisOutcome, HypothesisStrategy
from vibesys.metrics import AcceptedReading, MetricRow, PartialRow
from vibesys.orchestration.dynamic.models import StrategyReason
from vs_core.api.search import (
    AttemptId,
    CheckpointDecline,
    EvidenceRef,
    FailureReason,
    ObservationStatus,
    RejectionCode,
    ReplyError,
    RevisionRef,
    SettlementId,
    TurnFailureKind,
    Value,
    WaitRefusal,
)


class AgentName(StrEnum):
    """The agents of a dynamic run, as failures and logs name them.

    The values are the declared roles' names (`_roles.ROLES`), which `agent.toml` and the
    `dynamic-<name>` role ids use; a test keeps the two lists equal.
    """

    PLANNER = "orchestrator"
    IMPLEMENTER = "implementer"
    JUDGE = "judge"
    PROFILER = "profiler"


class WorkKind(StrEnum):
    """What a scheduled workstream does with its slot."""

    IMPLEMENT = "implement"
    PROFILE = "profile"


class UnreachableReason(StrEnum):
    """Typed reason a decision the search wants cannot be made."""

    PROFILE_CAPABILITY_NOT_OFFERED = "profile-capability-not-offered"
    RESUME_CAPABILITY_NOT_OFFERED = "resume-capability-not-offered"


class Unreachable(Value):
    """A decision not made, with the missing core contract named."""

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


class Expectation(Value):
    """What a planner expected of one workstream it scheduled, and what it meant to do next."""

    work_id: str
    expected: str
    if_it_succeeds: str
    if_it_fails: str


class HandoffNote(Value):
    """A planner's note to the planners after it, as its reply gave it, for what was scheduled.

    ``expectations`` name only workstreams the plan scheduled; ``dropped`` names the workstreams
    whose expectations were left out because they were not scheduled (or are not in the reply),
    so the note never says what to do next about work that never ran.
    """

    expectations: tuple[Expectation, ...] = ()
    doubts: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()


# -- why work ended -----------------------------------------------------------------------------


class BlockerKind(StrEnum):
    """Why an implementer turn ended without a candidate that reached measurement."""

    FAILED = "failed"
    REJECTED = "rejected"
    NO_REPLY = "no_reply"


class Blocker(Value):
    """One turn of a workstream that ended without a measurable candidate.

    ``FAILED`` is the implementer's own account (its summary and the next step it names).
    ``REJECTED`` is the reviewer's feedback on a candidate that was not measured. ``NO_REPLY`` is
    a turn that returned nothing usable: ``ended`` or ``turn_failure`` say how, and ``digest``
    fingerprints the diagnostic the executor printed. ``revision`` and ``digest`` are what the
    framework observed, and the only facts that make another turn worth asking.
    """

    kind: BlockerKind
    summary: str = ""
    next_step: str = ""
    revision: RevisionRef | None = None
    digest: str | None = None
    ended: FailureReason | None = None
    turn_failure: TurnFailureKind | None = None


class FailureKind(StrEnum):
    """Why a workstream ended without a measured candidate. Closed."""

    STOPPED = "stopped"
    """The operator stopped the run."""
    PARENT_UNVERIFIED = "parent-unverified"
    REFUSED = "refused"
    """Core refused a decision of the workstream; ``cause`` says why."""
    ENDED = "ended"
    """A wait of the workstream ended without a result; ``ended`` says why."""
    OUT_OF_BUDGET = "out-of-budget"
    TURN_FAILED = "turn-failed"
    """An agent's turn ended without a reply; ``role`` and ``turn_failure`` say how."""
    INVALID_REPLY = "invalid-reply"
    RENDER_FAILED = "render-failed"
    WAIT_REFUSED = "wait-refused"
    """An agent asked to wait for evaluations and the run could not honor it."""
    CHECKPOINT_DECLINED = "checkpoint-declined"
    NO_CHANGED_CANDIDATE = "no-changed-candidate"
    IMPLEMENTATION_FAILED = "implementation-failed"
    REVIEW_REJECTED = "review-rejected"
    NO_TRUSTED_EVIDENCE = "no-trusted-evidence"
    EVIDENCE_REFUSED = "evidence-refused"
    EVIDENCE_UNDECODABLE = "evidence-undecodable"
    PROFILE_CAPTURE_UNAVAILABLE = "profile-capture-unavailable"
    TURN_LIMIT = "turn-limit"
    """Too many turns ended without a candidate that reached measurement."""
    REPEATED_TURN = "repeated-turn"
    """The last turn left nothing an earlier turn had not, so another would repeat it."""


_FIELDS: dict[FailureKind, tuple[frozenset[str], frozenset[str]]] = {
    FailureKind.PARENT_UNVERIFIED: (frozenset({"revision"}), frozenset()),
    FailureKind.REFUSED: (frozenset({"cause"}), frozenset()),
    FailureKind.ENDED: (frozenset({"ended"}), frozenset({"role"})),
    FailureKind.TURN_FAILED: (frozenset({"role"}), frozenset({"turn_failure", "ended"})),
    FailureKind.INVALID_REPLY: (frozenset({"role"}), frozenset()),
    FailureKind.RENDER_FAILED: (frozenset({"role"}), frozenset()),
    FailureKind.WAIT_REFUSED: (frozenset({"role", "refusal"}), frozenset()),
    FailureKind.CHECKPOINT_DECLINED: (frozenset({"declined"}), frozenset()),
    FailureKind.NO_TRUSTED_EVIDENCE: (frozenset({"status"}), frozenset({"output"})),
    FailureKind.TURN_LIMIT: (frozenset({"turns", "limit", "blocker"}), frozenset()),
    FailureKind.REPEATED_TURN: (frozenset({"blocker"}), frozenset()),
}


class Failure(Value):
    """A typed account of why a workstream ended unmeasured, with only the fields its kind has."""

    kind: FailureKind
    role: AgentName | None = None
    cause: RejectionCode | None = None
    ended: FailureReason | None = None
    turn_failure: TurnFailureKind | None = None
    refusal: WaitRefusal | None = None
    declined: CheckpointDecline | None = None
    status: ObservationStatus | None = None
    output: str = ""
    """What an evaluator printed (a failed job's diagnostic), kept as captured data."""
    turns: int | None = None
    limit: int | None = None
    blocker: Blocker | None = None
    revision: RevisionRef | None = None

    @model_validator(mode="after")
    def fields_match_kind(self) -> "Failure":
        """Refuse a field that does not belong to the kind, or a kind missing its field."""
        required, optional = _FIELDS.get(self.kind, (frozenset(), frozenset()))
        present = {
            name
            for name in (
                "role",
                "cause",
                "ended",
                "turn_failure",
                "refusal",
                "declined",
                "status",
                "turns",
                "limit",
                "blocker",
                "revision",
            )
            if getattr(self, name) is not None
        } | ({"output"} if self.output else set())
        if missing := required - present:
            message = f"{self.kind.value} needs {sorted(missing)[0]}"
            raise ValueError(message)
        if extra := present - required - optional:
            message = f"{sorted(extra)[0]} does not belong to {self.kind.value}"
            raise ValueError(message)
        return self


# -- the record of a finished workstream ---------------------------------------------------------


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
    failure: Failure | None = None
    failure_tail: str = ""
    """What a failed trusted check printed (a crashed server's last log lines, say), whole."""
    settlement: SettlementId | None = None
    kept_active: bool = False
    """Whether the implementer asked to keep this direction and the lease allows it."""


class HypothesisRecord(Value):
    """A hypothesis's identity, lineage, strategic treatment and rounds."""

    hypothesis_id: str = Field(min_length=1)
    title: str
    hypothesis: str
    first_sequence: int = Field(gt=0)
    lineage_parent_id: str | None = None
    strategy: HypothesisStrategy = HypothesisStrategy.AVAILABLE
    reason_kind: StrategyReason | None = None
    reason: str = ""
    last_task: str = ""
    continuation_rounds: int = Field(default=0, ge=0)
    rounds: tuple[RoundRecord, ...] = ()


class Proof(Value):
    """The trusted evidence a measurement produced, held by value."""

    refs: tuple[EvidenceRef, ...]


class FinishedWork(Value):
    """What the search keeps of a workstream after it ended and its child was removed."""

    sequence: int = Field(gt=0)
    plan: WorkPlan
    parent: RevisionRef
    parent_hypothesis_id: str | None = None
    failure: Failure | None = None
    readings: tuple[AcceptedReading, ...] = ()
    proof: Proof | None = None
    """For a profile workstream: the evidence and decoded readings its history row shows."""


# -- what the planner is told about a reply that cannot be scheduled -------------------------------


class ViolationCode(StrEnum):
    """Why a planned entry or update cannot be applied. Closed."""

    OVER_CAPACITY = "over-capacity"
    DUPLICATE_ID = "duplicate-id"
    HYPOTHESIS_ID_USED = "hypothesis-id-used"
    HYPOTHESIS_ABANDONED = "hypothesis-abandoned"
    HYPOTHESIS_IN_FLIGHT = "hypothesis-in-flight"
    HYPOTHESIS_UNKNOWN = "hypothesis-unknown"
    HYPOTHESIS_TERMINAL = "hypothesis-terminal"
    HYPOTHESIS_BLOCKED = "hypothesis-blocked"
    REVISION_WITHOUT_PARENT = "revision-without-parent"
    CONTINUATION_HAS_PARENT = "continuation-has-parent"
    REVISION_NOT_OFFERED = "revision-not-offered"
    PARENT_NOT_BUILDABLE = "parent-not-buildable"
    PROFILE_NOT_OFFERED = "profile-not-offered"
    PROFILE_FIELDS_UNSUPPORTED = "profile-fields-unsupported"
    PROFILE_ID_USED = "profile-id-used"
    UPDATE_RUNNING = "update-running"
    UPDATE_DUPLICATE = "update-duplicate"
    UPDATE_UNKNOWN = "update-unknown"
    UPDATE_INCOMPLETE = "update-incomplete"
    EXPECTATION_UNKNOWN = "expectation-unknown"


class Alternative(Value):
    """One parent the planner may name instead: a hypothesis and its exact revision."""

    hypothesis_id: str
    revision: str


class PlanViolation(Value):
    """One reason a planned entry or update cannot be applied, with the facts it names.

    ``path`` locates the offending field in the reply. ``subject`` is the hypothesis, profile or
    revision ID the violation names; ``usable`` the parents that would have been accepted;
    ``requested`` and ``capacity`` the counts of an over-full portfolio; ``fields`` the profile
    fields the run's capture lacks (``supplied`` the ones it has).
    """

    path: tuple[str | int, ...]
    code: ViolationCode
    subject: str = ""
    revision: str = ""
    usable: tuple[Alternative, ...] = ()
    requested: int | None = None
    capacity: int | None = None
    fields: tuple[str, ...] = ()
    supplied: tuple[str, ...] = ()


class ProblemKind(StrEnum):
    """What was wrong with a planner reply as a whole. Closed."""

    UNPARSEABLE = "unparseable"
    VIOLATIONS = "violations"
    UNDERFILLED = "underfilled"
    NO_REPLY = "no-reply"


class PlanProblem(Value):
    """Why the planner's last reply was not accepted, as facts the correction template renders."""

    kind: ProblemKind
    errors: tuple[ReplyError, ...] = ()
    violations: tuple[PlanViolation, ...] = ()
    scheduled: int = Field(default=0, ge=0)
    wanted: int = Field(default=0, ge=0)
    ended: FailureReason | None = None
    turn_failure: TurnFailureKind | None = None

    @model_validator(mode="after")
    def fields_match_kind(self) -> "PlanProblem":
        """Refuse facts that do not belong to the kind."""
        belongs = {
            "errors": self.kind is ProblemKind.UNPARSEABLE,
            "violations": self.kind is ProblemKind.VIOLATIONS,
            "wanted": self.kind is ProblemKind.UNDERFILLED,
            "ended": self.kind is ProblemKind.NO_REPLY,
        }
        for name, allowed in belongs.items():
            if bool(getattr(self, name)) and not allowed:
                message = f"{name} does not belong to {self.kind.value}"
                raise ValueError(message)
        return self
