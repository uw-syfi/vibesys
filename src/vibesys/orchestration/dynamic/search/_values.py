"""The plain values the dynamic search keeps and passes around, with no behavior of their own.

Every value is a strict, frozen `Value` with closed fields. Reasons are enums with the typed
facts that go with them; the only free text is text an agent or an evaluator wrote (an
implementer's summary, a reviewer's feedback, what a failed check printed), carried as data for
a later prompt and never written by the search.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from vibesys.hypothesis import HypothesisOutcome, HypothesisStrategy
from vibesys.metrics import AcceptedReading, MetricRow, PartialRow
from vibesys.orchestration.dynamic.models import StrategyReason, UnappliedPart
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
    OUTLINER = "outliner"
    MERGER = "merger"


class WorkKind(StrEnum):
    """What a scheduled workstream does with its slot."""

    IMPLEMENT = "implement"
    PROFILE = "profile"
    MERGE = "merge"


class UnreachableReason(StrEnum):
    """Typed reason a decision the search wants cannot be made."""

    PROFILE_CAPABILITY_NOT_OFFERED = "profile-capability-not-offered"
    RESUME_CAPABILITY_NOT_OFFERED = "resume-capability-not-offered"


class Unreachable(Value):
    """A decision not made, with the missing core contract named."""

    reason: UnreachableReason
    subject: str


type StepId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")]
"""A plan step's id, as the task's ``[plan]`` table allows it; it names the step's workstreams."""


class Step(Value):
    """One step of the task's plan, as the search holds it (`vibesys.inputs.PlanStep`).

    The fields are the table's own; a test keeps the two equal.
    """

    id: StepId
    title: str = Field(min_length=1, max_length=200)
    documents: tuple[str, ...] = ()
    done_when: str = Field(min_length=1, max_length=2000)
    evaluate: Literal["none", "trusted"]
    """``none``: accepted on the judge's review of the implementer's own checks, never
    measured; ``trusted``: a milestone the trusted evaluation measures."""
    depends_on: tuple[StepId, ...] = ()


class CombinePick(Value):
    """One part a combine names: a hypothesis and the exact revision of it, as validated."""

    hypothesis_id: str
    revision: str


class CombineSource(Value):
    """One part of a combine as it was fixed at scheduling: its revision and its own change.

    ``built_on`` is the main revision the part's tree includes (`RunRecord.main_base_at`), so
    ``built_on..revision`` is the part's own change against main; ``metric`` is its trusted
    headline, when it had one.
    """

    hypothesis_id: str
    revision: RevisionRef
    built_on: RevisionRef
    metric: MetricRow | None = None


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
    step: Step | None = None
    """The plan step the workstream implements, in a run that follows the task's plan."""
    candidates: tuple[str, ...] = ()
    """For a merge: the hypotheses whose candidates it merges into main, in merge order."""
    instructions: str = ""
    """For a merge: how to combine the candidates."""
    watch_for: str = ""
    """For a merge or a combine: what the agent must check or be careful of."""
    combine: tuple[CombinePick, ...] = ()
    """For a combine: the parts, in the order to apply them. A combine is an implement plan of
    a new hypothesis that starts from main; a non-empty ``combine`` is what makes it one."""

    @property
    def measured(self) -> bool:
        """Whether its candidate gets a trusted measurement: all but an unevaluated plan step."""
        return self.step is None or self.step.evaluate == "trusted"


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
    REPLY_REJECTED = "reply_rejected"
    """The reply broke a rule only the run can check: ``stray`` names the offending entries."""


class StrayUnapplied(Value):
    """An ``unapplied`` entry of an implementer's reply that names no part of its combine."""

    index: int = Field(ge=0)
    """The entry's position in the reply's ``unapplied`` list."""
    hypothesis_id: str
    """The hypothesis the entry named."""


class Blocker(Value):
    """One turn of a workstream that ended without a measurable candidate.

    ``FAILED`` is the implementer's own account (its summary and the next step it names).
    ``REJECTED`` is the reviewer's feedback on a candidate that was not measured.
    ``REPLY_REJECTED`` is a reply the run refused (``stray`` says why). ``NO_REPLY`` is
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
    stray: tuple[StrayUnapplied, ...] = ()
    """For ``REPLY_REJECTED``: the ``unapplied`` entries that named no part of the combine."""


class DropReason(StrEnum):
    """Why a candidate waiting for an evaluation slot was dropped without being measured. Closed."""

    SUPERSEDED = "superseded"
    """A newer submission of the same line holds its work: the same workstream line, or a
    candidate built on it."""
    STOPPED = "stopped"
    """The run stopped (the operator, or its deadline) before the candidate was measured."""
    RESTARTED = "restarted"
    """The run restarted after a code change and could not give the workstream its workspace
    back: the candidate's code is kept as work in progress, unmeasured."""


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
    NOT_MEASURED = "not-measured"
    """The candidate waited for an evaluation slot and was dropped unmeasured; ``dropped`` says
    why, and ``superseded_by`` names the newer submission that holds its work."""
    MERGE_REFUSED = "merge-refused"
    """The merger could not follow the merge instructions and stopped; its reply says why."""
    MERGE_UNSOUND = "merge-unsound"
    """The merged commit did not pass the trusted accuracy check, so main did not move to it."""
    MERGE_REGRESSED = "merge-regressed"
    """The merged commit measured worse than main, or failed a benchmark check main passed, so
    main did not move to it."""


_FIELDS: dict[FailureKind, tuple[frozenset[str], frozenset[str]]] = {
    FailureKind.PARENT_UNVERIFIED: (frozenset({"revision"}), frozenset()),
    FailureKind.REFUSED: (frozenset({"cause"}), frozenset()),
    FailureKind.ENDED: (frozenset({"ended"}), frozenset({"role"})),
    FailureKind.TURN_FAILED: (frozenset({"role"}), frozenset({"turn_failure", "ended"})),
    FailureKind.INVALID_REPLY: (frozenset({"role"}), frozenset()),
    FailureKind.RENDER_FAILED: (frozenset({"role"}), frozenset()),
    FailureKind.WAIT_REFUSED: (frozenset({"role", "refusal"}), frozenset()),
    FailureKind.CHECKPOINT_DECLINED: (frozenset({"declined"}), frozenset()),
    FailureKind.NO_TRUSTED_EVIDENCE: (
        frozenset({"status"}),
        frozenset({"output", "operator_output"}),
    ),
    FailureKind.TURN_LIMIT: (frozenset({"turns", "limit", "blocker"}), frozenset()),
    FailureKind.REPEATED_TURN: (frozenset({"blocker"}), frozenset()),
    FailureKind.NOT_MEASURED: (frozenset({"dropped"}), frozenset({"superseded_by"})),
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
    operator_output: str | None = None
    """Credential-safe projection of output for operator-facing events only."""
    turns: int | None = None
    limit: int | None = None
    blocker: Blocker | None = None
    revision: RevisionRef | None = None
    dropped: DropReason | None = None
    superseded_by: str | None = None

    @model_validator(mode="after")
    def fields_match_kind(self) -> "Failure":
        """Refuse a field that does not belong to the kind, or a kind missing its field."""
        required, optional = _FIELDS.get(self.kind, (frozenset(), frozenset()))
        present = (
            {
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
                    "dropped",
                    "superseded_by",
                )
                if getattr(self, name) is not None
            }
            | ({"output"} if self.output else set())
            | ({"operator_output"} if self.operator_output is not None else set())
        )
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
    unapplied: tuple[UnappliedPart, ...] = ()
    """For a combine: what the implementer said it could not apply, naming one of its parts."""


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
    combined_from: tuple[CombineSource, ...] = ()
    """For a hypothesis a combine started: its parts, as they were fixed at scheduling."""


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
    main_base: RevisionRef | None = None
    """The main revision the workstream's tree included: the main it caught up with, else the
    one its parent's tree included. Git history does not say it: under read-only git a catch-up
    applies main's change without recording main as an ancestor. None in a record kept by an
    older build."""


# -- the run's main revision and its merges -------------------------------------------------------


class MergeSource(Value):
    """One candidate a merge applies: the hypothesis and the exact revision, fixed at scheduling.

    ``built_on`` is the main revision the candidate's tree includes, so ``built_on..revision`` is
    its own change; None only in a merge scheduled by an older build.
    """

    hypothesis_id: str
    revision: RevisionRef
    built_on: RevisionRef | None = None


class MergeOutcome(StrEnum):
    """How a merge workstream ended. Closed."""

    MERGED = "merged"
    """The merged commit passed the trusted accuracy check, measured no worse than main, and main
    advanced to it."""
    REFUSED = "refused"
    """The merger could not follow the instructions and stopped: main is unchanged."""
    FAILED = "failed"
    """The merge ended without a sound commit (a lost turn, a stop, a failed check, a regression):
    main is unchanged; ``failure`` says why."""


class MergeRecord(Value):
    """A finished merge workstream, as the planner and the handoff files are told of it."""

    merge_id: str
    sequence: int = Field(gt=0)
    candidates: tuple[MergeSource, ...]
    base: RevisionRef
    """The main revision the merge started from."""
    outcome: MergeOutcome
    summary: str = ""
    """The merger's account of what it did."""
    reason: str = ""
    """Why the merger refused; empty unless it did."""
    revision: RevisionRef | None = None
    """The merger's commit, when it made one; the new main when the merge merged."""
    accuracy_passed: bool | None = None
    benchmark_passed: bool | None = None
    metrics: tuple[MetricRow, ...] = ()
    """What the trusted measurement of the commit read; empty when it was not measured."""
    main_metrics: tuple[MetricRow, ...] = ()
    """Main's headline the commit was compared with; empty when it was not compared."""
    main_benchmark_passed: bool | None = None
    """Whether main passed its benchmark check when the commit was compared with it."""
    failure: Failure | None = None
    """Why a failed merge ended."""


class MainMeasurement(Value):
    """The trusted measurement of the run's main, and how it compares with main before it."""

    revision: RevisionRef
    merge_id: str
    """The merge that made this main."""
    accuracy_passed: bool | None = None
    benchmark_passed: bool | None = None
    metrics: tuple[MetricRow, ...] = ()
    previous: tuple[MetricRow, ...] = ()
    """What main measured before this merge (the input's measurement before the first merge)."""


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
    OUTSIDE_PLAN = "outside-plan"
    """The run follows the task's plan, and the entry is not a repair of one of its failed steps."""
    EXPECTATION_UNKNOWN = "expectation-unknown"
    RANKING_UNKNOWN = "ranking-unknown"
    RANKING_DUPLICATE = "ranking-duplicate"
    MERGE_ID_USED = "merge-id-used"
    MERGE_IN_FLIGHT = "merge-in-flight"
    MERGE_CANDIDATE_REPEATED = "merge-candidate-repeated"
    MERGE_CANDIDATE_IS_MAIN = "merge-candidate-is-main"
    MERGE_CANDIDATE_MERGED = "merge-candidate-merged"
    MERGE_CANDIDATE_NOT_BETTER = "merge-candidate-not-better"
    MERGE_BASELINE_PENDING = "merge-baseline-pending"
    MERGE_UNCHECKABLE = "merge-uncheckable"
    COMBINE_PARENT_REPEATED = "combine-parent-repeated"
    """A combine names one hypothesis twice: each part is a different hypothesis."""


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
