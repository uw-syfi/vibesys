"""What a workstream establishes and leaves behind, as plain typed values.

A workstream is told what to do (`WorkstreamStart`), establishes a `Result` and `Evidence`, and
ends with a `WorkstreamDone` the search folds into its record. Whatever stops it early is a
`WorkEndedError` carrying the typed `Failure`; the helpers here turn what core reported (a
`Failed` event, a blocked turn) into that failure and what a finished workstream left into the
round, parent and settlement the search records. Nothing here awaits or reads the run.
"""

from dataclasses import dataclass, field

from vibesys.candidates.api import STOPPED as STOPPED_TEXT
from vibesys.candidates.api import (
    Baseline,
    assessment,
    gates_of,
    improves_on,
    input_metrics,
    measurement,
    settle_for,
    space_of,
)
from vibesys.hypothesis import HypothesisOutcome
from vibesys.hypothesis.cadence import keeps_hypothesis_active
from vibesys.metrics import AcceptedReading, MetricRow, reading_of
from vibesys.orchestration.dynamic.models import UnappliedPart
from vibesys.orchestration.dynamic.search._config import DynamicConfig
from vibesys.orchestration.dynamic.search._parents import ParentSnapshot
from vibesys.orchestration.dynamic.search._values import (
    AgentName,
    Blocker,
    BlockerKind,
    CombineSource,
    DropReason,
    Failure,
    FailureKind,
    MergeOutcome,
    MergeSource,
    Proof,
    RoundRecord,
    Unreachable,
    UnreachableReason,
    WorkKind,
    WorkPlan,
)
from vs_core.api.search import (
    AssessmentKind,
    AttemptBudget,
    AttemptId,
    AttemptRef,
    EvidenceKind,
    Failed,
    FailureReason,
    InvocationRef,
    ItemId,
    RetryPolicy,
    RevisionRef,
    Settle,
    Settlement,
    SpanId,
    TaskTerms,
    Value,
    WaitRefusal,
    Workspace,
    WorkspaceMode,
    WorkspacePlan,
    attempt_id,
    item_id,
)

# -- what a workstream is told ---------------------------------------------------------------------


class WorkstreamStart(Value):
    """One scheduled workstream: its plan, and the parent it builds on, frozen at scheduling."""

    key: str
    sequence: int
    plan: WorkPlan
    parent: RevisionRef
    parent_hypothesis_id: str | None
    prior: RoundRecord | None
    """The previous round of the same hypothesis, when this workstream continues it."""
    config: DynamicConfig
    baseline: Baseline | None
    """The input's measurement when the workstream started; None while it was still being measured."""
    merge: tuple[MergeSource, ...] = ()
    """For a merge: the candidates to merge into ``parent`` (main), in order."""
    combine: tuple[CombineSource, ...] = ()
    """For a combine: the parts to apply onto ``parent`` (main), in order, fixed at scheduling."""
    rebase_onto: RevisionRef | None = None
    """The run's main, when it advanced past the base of the hypothesis this continues."""
    parent_main: RevisionRef | None = None
    """Where ``parent``'s own change against main starts (`RunRecord.main_base_at`): a main, or a
    merged candidate of its lineage. A catch-up applies main's change from it to ``rebase_onto``.
    None in a start scheduled by an older build."""
    started_by: tuple[str, ...] = ()
    """The agents whose reply planned this workstream (the planner, or the outliner of a step):
    they start its agents. Empty when no agent's reply is known to have planned it."""

    @property
    def main_base(self) -> RevisionRef | None:
        """Where this workstream's own change starts: its catch-up main, else its parent's start."""
        return self.rebase_onto or self.parent_main

    @property
    def attempt(self) -> AttemptId:
        """The identity of the attempt this workstream opens."""
        return attempt_id(self.plan.work_id, self.sequence)

    @property
    def item(self) -> ItemId:
        """The scientific item the attempt works on."""
        return item_id(self.plan.work_id, self.sequence)

    @property
    def implements(self) -> bool:
        """Whether the workstream implements a hypothesis, rather than merging or looking."""
        return self.plan.kind is WorkKind.IMPLEMENT

    @property
    def workspace(self) -> WorkspacePlan:
        """An implementer or a merger works in an isolated child of the parent; a profiler reads it."""
        writes = self.plan.kind in {WorkKind.IMPLEMENT, WorkKind.MERGE}
        mode = WorkspaceMode.ISOLATED_CHILD if writes else WorkspaceMode.READ_ONLY_REVISION
        return WorkspacePlan(mode=mode, base=self.parent)

    @property
    def review_always(self) -> bool:
        """Whether every candidate is reviewed: a plan step accepted on review alone."""
        return not self.plan.measured

    @property
    def turns(self) -> int:
        """How many paid agent turns the workstream may take: one, unless it implements."""
        return self.config.max_unmeasured_turns if self.implements else 1

    @property
    def budget(self) -> AttemptBudget:
        """The attempt's admission charge and its paid turns."""
        return AttemptBudget(admission_charge=1, paid_invocation_limit=self.turns)

    @property
    def retries(self) -> RetryPolicy:
        """How many implementer turns the workstream may take."""
        return RetryPolicy(max_tries=self.turns)


# -- what a workstream establishes ---------------------------------------------------------------------


class Candidate(Value):
    """What the implementer's reply and core's checkpoint established."""

    outcome: HypothesisOutcome
    summary: str
    next_step: str = ""
    revision: RevisionRef | None = None
    """The revision core retained for the turn; None when no checkpoint was awaited or none exists."""
    unapplied: tuple[UnappliedPart, ...] = ()
    """For a combine: what the implementer could not apply, naming only the combine's parts."""
    unattributed: tuple[UnappliedPart, ...] = ()
    """The reply's ``unapplied`` entries that named no part of the combine (`RoundRecord`)."""


class Verdict(Value):
    """The judge's decision on a candidate, with the turn it came from."""

    passed: bool
    invocation: InvocationRef


class Result(Value):
    """A candidate and, once reviewed, the verdict on it."""

    candidate: Candidate
    verdict: Verdict | None = None

    def unreviewed(self) -> "Result":
        """The same candidate, to be reviewed again."""
        return self.model_copy(update={"verdict": None})


class Unmeasured(Value):
    """A turn that ended without a candidate that can be measured: why, and what it left."""

    blocker: Blocker
    result: Result | None = None


class MergeReport(Value):
    """What the merger answered, with the commit core checkpointed when it merged."""

    outcome: MergeOutcome
    summary: str = ""
    reason: str = ""
    revision: RevisionRef | None = None


class Evidence(Value):
    """The trusted readings of a candidate, and the evidence they were decoded from."""

    readings: tuple[AcceptedReading, ...] = ()
    proof: Proof | None = None


class Closing(Value):
    """What the close of the workspace asserts: the finished attempt the settlement judges.

    It reads as the `FinishedAttempt` that candidate policy settles.
    """

    result: Result | None = None
    evidence: Evidence = Evidence()
    cause: Failure | None = None
    unreachable: Unreachable | None = None
    merge: MergeReport | None = None
    """What a merge workstream's merger answered."""

    @property
    def readings(self) -> tuple[AcceptedReading, ...]:
        """The decoded trusted evaluations of the attempt's candidate."""
        return self.evidence.readings

    @property
    def candidate(self) -> RevisionRef | None:
        """The revision the attempt retained, if any: a merge's commit, else the candidate's."""
        if self.merge is not None:
            return self.merge.revision
        return None if self.result is None else self.result.candidate.revision

    def scored(self, plan: WorkPlan) -> bool:
        """Whether settling it compares a candidate with the input, so needs its measurement.

        Only a measured candidate is compared. A plan step accepted on its own checks
        (``evaluate = "none"``) never is, and a closing with a failure cause is ineligible however
        its candidate measured: both settle at once, without waiting for the input's measurement.
        """
        return plan.measured and self.candidate is not None and self.cause is None

    @property
    def review_passed(self) -> bool | None:
        """The judge's verdict, or None when no review ran."""
        verdict = None if self.result is None else self.result.verdict
        return None if verdict is None else verdict.passed

    @property
    def judge_invocation(self) -> InvocationRef | None:
        """The judge turn the verdict came from."""
        verdict = None if self.result is None else self.result.verdict
        return None if verdict is None else verdict.invocation

    @property
    def outcome(self) -> HypothesisOutcome | None:
        """The implementer's own account of its result."""
        return None if self.result is None else self.result.candidate.outcome

    @property
    def failure(self) -> str | None:
        """The recorded failure as candidate policy reads it: the operator's stop, or its kind."""
        if self.cause is None:
            return None
        return STOPPED_TEXT if self.cause.kind is FailureKind.STOPPED else self.cause.kind.value


@dataclass
class Work:
    """What a workstream holds while it works, so a failure or a stop still settles with it."""

    carried: Result | None = None
    """The latest candidate the implementer made, reviewed or not."""
    workspace: Workspace | None = None
    """The workspace once it opened; None before, and when the workstream never got one."""
    baseline: Baseline | None = None
    """The input's measurement once the workstream has it (see `_measure.the_input`)."""
    input_wait_since: float | None = None
    """When, on the run clock, the workstream first waited on something that needs the input."""
    overdue_reported: bool = False
    """Whether its wait for the input has passed the run's limit (`_measure.while_input_pending`)."""
    merged: MergeReport | None = None
    """The merger's commit, once it made one, so a failed measurement still settles with it."""
    gate: tuple[SpanId, str] | None = None
    """The span of the trusted measurement once it was issued, and the stages it measures."""

    def ended(self, ended: "WorkEndedError") -> Closing:
        """The closing of work that did not finish: what it carried, with the failure."""
        return Closing(
            result=self.carried,
            evidence=Evidence(proof=ended.proof),
            cause=ended.failure,
            unreachable=ended.unreachable,
            merge=self.merged,
        )

    def stopped(self) -> Closing:
        """The closing of work the operator stopped."""
        return Closing(result=self.carried, cause=STOPPED, merge=self.merged)


class WorkEndedError(Exception):
    """A workstream cannot go on: it ends with this failure and settles with what it has."""

    def __init__(
        self,
        failure: Failure,
        *,
        proof: Proof | None = None,
        unreachable: Unreachable | None = None,
    ) -> None:
        """Carry the failure and the facts that settle with it."""
        super().__init__(failure.kind.value)
        self.failure = failure
        self.proof = proof
        self.unreachable = unreachable


STOPPED = Failure(kind=FailureKind.STOPPED)
"""How an operator stop ends a workstream."""
RESTARTED = Failure(kind=FailureKind.NOT_MEASURED, dropped=DropReason.RESTARTED)
"""How a workstream ends whose workspace a restart after a code change could not give back."""


@dataclass(frozen=True)
class WorkstreamDone:
    """The workstream is over; the search records it.

    ``refunded`` says the slot was never used (the workspace never opened). ``withheld`` is a
    parent revision that could not be verified and is never offered again. ``settlement`` is
    core's settlement of the workspace; None if the workspace never opened or core took none.
    """

    start: WorkstreamStart
    failure: Failure | None
    generation: int = 0
    refunded: bool = False
    withheld: RevisionRef | None = None
    settlement: Settlement | None = None
    result: Result | None = None
    evidence: Evidence = field(default_factory=Evidence)
    unreachable: Unreachable | None = None
    merge: MergeReport | None = None

    @classmethod
    def stopped(cls, start: WorkstreamStart) -> "WorkstreamDone":
        """Stopped before it had a workspace: the slot counts as used."""
        return cls(start, failure=STOPPED)

    @classmethod
    def not_opened(
        cls, start: WorkstreamStart, failure: Failure, *, withheld: RevisionRef | None = None
    ) -> "WorkstreamDone":
        """No workspace was admitted: the slot is refunded."""
        return cls(start, failure=failure, refunded=True, withheld=withheld)

    @classmethod
    def lost(
        cls,
        start: WorkstreamStart,
        workspace: AttemptRef,
        result: Result | None,
        merge: MergeReport | None = None,
    ) -> "WorkstreamDone":
        """Resumed after a code change without its workspace: the restart settled it.

        The restart settled it as the workstream's salvage said: a candidate's or a merger's
        code is kept as work in progress, unmeasured (`salvage_of`); its settlement reached no
        flow, so main does not move to a lost merge's commit.
        """
        return cls(
            start,
            failure=RESTARTED,
            generation=workspace.generation,
            result=result,
            merge=merge,
        )

    @classmethod
    def closed(
        cls, start: WorkstreamStart, workspace: Workspace | None, closing: Closing
    ) -> "WorkstreamDone":
        """The workspace was closed with what it established; none means a stop came first."""
        if workspace is None:
            return cls.stopped(start)
        return cls(
            start,
            failure=closing.cause or _refusal(workspace),
            generation=workspace.ref.generation,
            settlement=workspace.settlement,
            result=closing.result,
            evidence=closing.evidence,
            unreachable=closing.unreachable,
            merge=closing.merge,
        )


# -- what a workstream asserts when it closes ----------------------------------------------------------


def assertion(
    w: WorkstreamStart, closing: Closing, terms: TaskTerms, baseline: Baseline | None
) -> Settle:
    """The assessments, eligibility and retention the closing proposes for the workspace.

    A profile workstream measures a revision without proposing one, so it settles as ineligible
    and discarded. A candidate, an implementer's or a merger's commit, is retained when it passed
    accuracy and is eligible when it passed the task's gates and beats the input, so a closing
    that is scored (`Closing.scored`) needs the input's measurement (``baseline``).
    """
    if w.plan.kind is WorkKind.PROFILE:
        profile = reading_of(closing.readings, EvidenceKind.PROFILING)
        return Settle(
            assessments=assessment(AssessmentKind.PROFILING, profile, None),
            eligible=False,
            retention="discard",
            outcome="failed" if closing.cause is not None else "succeeded",
            candidate=None,
        )
    if not w.plan.measured:
        return _unmeasured_step(closing, terms)
    if closing.cause is not None and closing.cause.kind is FailureKind.NOT_MEASURED:
        return _not_measured(closing, terms)
    if closing.scored(w.plan) and baseline is None:
        message = "a candidate is scored only once the input's measurement is known"
        raise ValueError(message)
    return settle_for(
        closing,
        gates=gates_of(terms),
        beats_input=beats_input(closing, terms, baseline),
        unsettled=UNSETTLED,
    )


def headline(closing: Closing) -> MetricRow | None:
    """The candidate's headline benchmark value, when it was measured."""
    benchmark = reading_of(closing.readings, EvidenceKind.BENCHMARK)
    return None if benchmark is None else benchmark.headline()


def input_headline(baseline: Baseline | None) -> MetricRow | None:
    """The input's headline value: the first number of the bar a candidate must beat, if any."""
    bar = input_metrics(baseline)
    return bar[0] if bar else None


def beats_input(closing: Closing, terms: TaskTerms, baseline: Baseline | None) -> bool:
    """Whether the candidate's headline improves on the input's bar (no bar is beaten).

    The bar is `input_metrics`: a measured input's benchmark, or that of one that failed only
    its accuracy check. With no bar the candidate is judged on the task's gates alone, and a
    run that adopts it says so (`RunRecord.ending`).
    """
    return improves_on(
        measurement(headline(closing)),
        measurement(input_headline(baseline)),
        space=space_of(terms),
        when_input_unmeasured=True,
    )


UNSETTLED = frozenset({HypothesisOutcome.BLOCKED, HypothesisOutcome.IMPLEMENTATION_FAILED})
"""The implementer's own outcomes that make a candidate ineligible however it measured."""


def salvage_of(closing: Closing, terms: TaskTerms) -> Settle:
    """How a workspace settles if a restart cannot give it back to its workstream.

    ``closing`` carries what the workstream had made: its latest candidate, or a merger's
    commit. It settles as a candidate dropped unmeasured: never eligible, its code kept as work
    in progress.
    """
    return _not_measured(closing.model_copy(update={"cause": RESTARTED}), terms)


def _unmeasured_step(closing: Closing, terms: TaskTerms) -> Settle:
    """The settlement of a plan step accepted on review alone (``evaluate = "none"``).

    Its candidate is never measured, so never winner-eligible. When the judge accepted it and
    nothing failed, it is retained as work in progress: the revision the steps that depend on it
    build on. Anything else is discarded, as an unmeasured candidate is.
    """
    settled = settle_for(closing, gates=gates_of(terms), beats_input=False, unsettled=UNSETTLED)
    accepted = (
        closing.candidate is not None
        and closing.cause is None
        and closing.review_passed is True
        and closing.outcome not in UNSETTLED
    )
    if not accepted:
        return settled.model_copy(update={"eligible": False})
    return settled.model_copy(
        update={"retention": "wip", "candidate": closing.candidate, "eligible": False}
    )


def _not_measured(closing: Closing, terms: TaskTerms) -> Settle:
    """The settlement of a candidate dropped from the evaluation queue unmeasured.

    It is never eligible, but the code is kept as work in progress: a newer candidate that
    superseded it was built on it, and a plan step may still build on it.
    """
    settled = settle_for(closing, gates=gates_of(terms), beats_input=False, unsettled=UNSETTLED)
    if closing.candidate is None:
        return settled.model_copy(update={"eligible": False, "outcome": "cancelled"})
    return settled.model_copy(
        update={
            "retention": "wip",
            "candidate": closing.candidate,
            "eligible": False,
            "outcome": "cancelled",
        }
    )


# -- how a workstream ends ---------------------------------------------------------------------------


def _refusal(workspace: Workspace) -> Failure | None:
    """Why core would not settle the workspace as the workstream asked, when it would not."""
    refused = workspace.refused
    return None if refused is None else failure_of(refused, None)


def failure_of(event: Failed, role: AgentName | None) -> Failure:  # noqa: PLR0911  # LW-220433 [PLR0911]; one return per wait-failure reason keeps the mapping to the typed failure exhaustive and visible.
    """The typed account of a wait that ended without a result."""
    match event.reason:
        case FailureReason.REFUSED:
            return Failure(kind=FailureKind.REFUSED, cause=event.cause)
        case FailureReason.OUT_OF_BUDGET:
            return Failure(kind=FailureKind.OUT_OF_BUDGET)
        case FailureReason.INVALID_REPLY:
            return Failure(kind=FailureKind.INVALID_REPLY, role=role)
        case FailureReason.RENDER_FAILED:
            return Failure(kind=FailureKind.RENDER_FAILED, role=role)
        case FailureReason.WAIT_REFUSED:
            return Failure(kind=FailureKind.WAIT_REFUSED, role=role, refusal=event.refusal)
        case FailureReason.TURN_FAILED:
            return Failure(kind=FailureKind.TURN_FAILED, role=role, turn_failure=event.turn_failure)
        case (
            FailureReason.TIMED_OUT
            | FailureReason.QUEUE_BUDGET_SPENT
            | FailureReason.WORKSPACE_CLOSED
            | FailureReason.OWNER_GONE
            | FailureReason.LOST
            | FailureReason.MEASUREMENT_UNTRUSTED
            | FailureReason.ATTEMPTS_EXHAUSTED
        ):
            return Failure(kind=FailureKind.ENDED, ended=event.reason, role=role)


def ended_by(
    event: Failed, role: AgentName | None, *, plan: WorkPlan | None = None
) -> WorkEndedError:
    """The end of a workstream whose wait failed.

    A turn core could not suspend for lack of the resume capability also records that fact,
    named by the plan it was for.
    """
    failure = failure_of(event, role)
    missing = None
    if (
        plan is not None
        and failure.kind is FailureKind.WAIT_REFUSED
        and failure.refusal is WaitRefusal.NOT_OFFERED
    ):
        missing = Unreachable(
            reason=UnreachableReason.RESUME_CAPABILITY_NOT_OFFERED, subject=plan.work_id
        )
    return WorkEndedError(failure, unreachable=missing)


def blocked_failure(blocker: Blocker) -> Failure:
    """Why a workstream that may not retry ended, from the last turn that gave nothing."""
    match blocker.kind:
        case BlockerKind.FAILED:
            return Failure(kind=FailureKind.IMPLEMENTATION_FAILED)
        case BlockerKind.REJECTED:
            return Failure(kind=FailureKind.REVIEW_REJECTED)
        case BlockerKind.REPLY_REJECTED:
            return Failure(kind=FailureKind.INVALID_REPLY, role=AgentName.IMPLEMENTER)
        case BlockerKind.NO_REPLY:
            if blocker.turn_failure is not None:
                return Failure(
                    kind=FailureKind.TURN_FAILED,
                    role=AgentName.IMPLEMENTER,
                    turn_failure=blocker.turn_failure,
                )
            return Failure(
                kind=FailureKind.ENDED,
                role=AgentName.IMPLEMENTER,
                ended=blocker.ended or FailureReason.TIMED_OUT,
            )


# -- what a finished workstream leaves behind -------------------------------------------------------


def _failing_tail(done: WorkstreamDone) -> str:
    """What the round's failed trusted check printed, else what a failed evaluation printed.

    A benchmark that crashed the server leaves its output in the reading's feedback; that text,
    not the agent's summary, is what tells the next agent why the candidate failed.
    """
    accuracy = reading_of(done.evidence.readings, EvidenceKind.CORRECTNESS)
    benchmark = reading_of(done.evidence.readings, EvidenceKind.BENCHMARK)
    for seen in (benchmark, accuracy):
        if seen is not None and not seen.passed and seen.feedback:
            return seen.feedback
    return "" if done.failure is None else done.failure.output


def round_of(done: WorkstreamDone) -> RoundRecord:
    """The round an implement workstream adds to its hypothesis."""
    start = done.start
    accuracy = reading_of(done.evidence.readings, EvidenceKind.CORRECTNESS)
    benchmark = reading_of(done.evidence.readings, EvidenceKind.BENCHMARK)
    result = done.result
    outcome = None if result is None else result.candidate.outcome
    next_step = "" if result is None else result.candidate.next_step
    kept = keeps_hypothesis_active(
        outcome=outcome,
        next_step=next_step,
        continuation_rounds=0,
        max_continuation_rounds=start.config.max_continuation_rounds,
    )
    settlement = done.settlement
    return RoundRecord(
        sequence=start.sequence,
        attempt=start.attempt,
        outcome=outcome,
        summary="" if result is None else result.candidate.summary,
        review_passed=None if result is None or result.verdict is None else result.verdict.passed,
        candidate=None if settlement is None else settlement.candidate,
        accuracy_passed=None if accuracy is None else accuracy.passed,
        benchmark_passed=None if benchmark is None else benchmark.passed,
        metrics=() if benchmark is None else benchmark.metrics,
        partial=None if benchmark is None else benchmark.partial,
        eligible=settlement is not None and settlement.eligible,
        failure=done.failure,
        failure_tail=_failing_tail(done),
        settlement=None if settlement is None else settlement.settlement_id,
        kept_active=kept,
        unapplied=() if result is None else result.candidate.unapplied,
        unattributed=() if result is None else result.candidate.unattributed,
    )


def snapshot_of(done: WorkstreamDone) -> ParentSnapshot | None:
    """The exact candidate offered as a parent: one core retained, with its accuracy proven."""
    accuracy = reading_of(done.evidence.readings, EvidenceKind.CORRECTNESS)
    settlement = done.settlement
    if (
        settlement is None
        or settlement.retention != "candidate"
        or settlement.candidate is None
        or done.start.plan.kind is WorkKind.PROFILE
        or accuracy is None
        or not accuracy.passed
        or done.evidence.proof is None
    ):
        return None
    return ParentSnapshot(
        hypothesis_id=done.start.plan.work_id,
        generation=done.generation,
        revision=settlement.candidate,
        accuracy=accuracy,
        benchmark=reading_of(done.evidence.readings, EvidenceKind.BENCHMARK),
        proof=done.evidence.proof.refs,
        submission_index=done.start.sequence,
        change_summary=None if done.result is None else done.result.candidate.summary or None,
    )
