"""The implementer's turns and the judge's review: until a candidate is ready to measure."""

from vibesys.candidates.api import Baseline, review_evaluations
from vibesys.hypothesis import HypothesisConfig, HypothesisOutcome
from vibesys.hypothesis.cadence import review_due
from vibesys.metrics import AcceptedReading, accept_readings
from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search._briefs import implement_prompt, review_prompt
from vibesys.orchestration.dynamic.search._measure import input_so_far
from vibesys.orchestration.dynamic.search._operations import EvidenceReadings
from vibesys.orchestration.dynamic.search._outcomes import (
    Candidate,
    Result,
    Unmeasured,
    Verdict,
    Work,
    WorkEndedError,
    WorkstreamStart,
    blocked_failure,
    ended_by,
)
from vibesys.orchestration.dynamic.search._progress import refusal
from vibesys.orchestration.dynamic.search._resume import ResumeStage, WorkstreamResume, mark
from vibesys.orchestration.dynamic.search._values import (
    AgentName,
    Blocker,
    BlockerKind,
    Failure,
    FailureKind,
    StrayUnapplied,
)
from vibesys.search_spans.api import turn_label
from vs_core.api.search import (
    Again,
    Declined,
    EvidenceRef,
    Failed,
    FailureReason,
    Flow,
    GaveUp,
    RetryPolicy,
    RevisionRef,
    Session,
    Tries,
    TurnFinished,
    Workspace,
)


async def implement_until_measurable(  # noqa: PLR0913  # lint-waiver: LW-271018 [PLR0913]; each argument is a distinct fact of the implementation (the flow, what it works on, what it carries, who it asks, and where it resumes).
    # > Bundling them in a record only renames the parameters; the workstream already holds each.
    ctx: Flow,
    w: WorkstreamStart,
    ws: Workspace,
    work: Work,
    sessions: "Conversation",
    at: WorkstreamResume | None,
) -> Result:
    """Ask the implementer (and the judge, when a review is due) until a candidate is ready.

    A turn that ends without a measurable candidate is asked again while the workstream's retry
    policy and the progress policy allow it; the workstream ends otherwise. A workstream resumed
    after a code change goes on with the turns it had left, and reviews the candidate its
    implementer had made if that was still to be reviewed.
    """
    earlier = () if at is None else at.blockers
    unreviewed = [at.result if at is not None and at.at(ResumeStage.IMPLEMENTED) else None]
    if len(earlier) >= w.turns and unreviewed[0] is None:
        raise WorkEndedError(blocked_failure(earlier[-1]))

    async def turn(tries: Tries[Blocker]) -> Result | Again[Blocker]:
        made, unreviewed[0] = unreviewed[0], None
        return await take_turn(ctx, w, ws, work, sessions, (*earlier, *tries.reasons), made)

    tries = RetryPolicy(max_tries=max(w.turns - len(earlier), 1))
    match await ctx.retry(turn, tries):
        case Result() as result:
            return result
        case GaveUp(reasons=blockers):
            raise WorkEndedError(blocked_failure(blockers[-1]))


class Conversation:
    """The two agents of one workstream: each is one conversation for the whole workstream."""

    def __init__(self, implementer: Session, judge: Session) -> None:
        """Hold the implementer's and the judge's sessions."""
        self.implementer = implementer
        self.judge = judge


async def take_turn(  # noqa: PLR0913  # LW-220431 [PLR0913]; each argument is a distinct fact of the turn (the flow, what it works on, what it carries and who it asks); bundling them in a throwaway record only renames the parameters.
    ctx: Flow,
    w: WorkstreamStart,
    ws: Workspace,
    work: Work,
    sessions: Conversation,
    earlier: tuple[Blocker, ...],
    made: Result | None = None,
) -> Result | Again[Blocker]:
    """One round with the implementer and the judge: a candidate, or why to ask again.

    ``made`` is a candidate the implementer made before a restart: it is reviewed, not made
    again. A candidate to review, and the reason to ask again, are checkpointed.
    """
    work.carried = None if work.carried is None else work.carried.unreviewed()
    turn: Result | Unmeasured
    if made is None:
        baseline = input_so_far(ctx, w)
        turn = await implementer_turn(sessions.implementer, w, work, earlier, baseline=baseline)
        if isinstance(turn, Result) and _review_due(w, turn):
            mark(ctx, w, work, ResumeStage.IMPLEMENTED, blockers=earlier, result=turn)
    else:
        turn = made
    if isinstance(turn, Result) and _review_due(w, turn):
        turn = await review(ctx, w, ws, work, sessions.judge, turn)
    if isinstance(turn, Result):
        return turn
    work.carried = turn.result
    blockers = (*earlier, turn.blocker)
    if (refused := refusal(blockers, w.config, w.parent)) is not None:
        raise WorkEndedError(refused)
    mark(ctx, w, work, ResumeStage.WORKING, blockers=blockers)
    return Again(reason=turn.blocker)


async def implementer_turn(
    implementer: Session,
    w: WorkstreamStart,
    work: Work,
    blockers: tuple[Blocker, ...],
    *,
    baseline: Baseline | None,
) -> Result | Unmeasured:
    """One implementer turn: the candidate it made, or what kept it from making one.

    ``baseline`` is the input's measurement as known when the turn is asked (None while it has
    not landed); the prompt says it is pending, or why it failed.
    """
    prompt = implement_prompt(w, blockers, baseline=baseline)
    match await implementer.turn(prompt, label=turn_label(implementer)):
        case TurnFinished(reply=ImplementerResult() as reply, checkpoint=checkpoint):
            return _candidate_of(w, work, reply, checkpoint)
        case Failed(reason=FailureReason.TIMED_OUT | FailureReason.TURN_FAILED) as failed:
            ended = failed.reason if failed.reason is FailureReason.TIMED_OUT else None
            return Unmeasured(
                blocker=Blocker(
                    kind=BlockerKind.NO_REPLY,
                    ended=ended,
                    turn_failure=failed.turn_failure,
                    digest=failed.digest,
                ),
                result=work.carried,
            )
        case Failed() as failed:
            raise ended_by(failed, AgentName.IMPLEMENTER, plan=w.plan)
        case TurnFinished() as unexpected:
            message = f"the implementer answered with {type(unexpected.reply).__name__}"
            raise TypeError(message)


def _candidate_of(
    w: WorkstreamStart,
    work: Work,
    reply: ImplementerResult,
    checkpoint: object,
) -> Result | Unmeasured:
    """Fold the reply with core's checkpoint of the turn, which a non-blocked reply awaits."""
    revision: RevisionRef | None = None
    if reply.produces_revision():
        if isinstance(checkpoint, Declined):
            work.carried = None
            raise WorkEndedError(
                Failure(kind=FailureKind.CHECKPOINT_DECLINED, declined=checkpoint.reason)
            )
        revision = getattr(checkpoint, "revision", None)
    result = Result(
        candidate=Candidate(
            outcome=reply.outcome,
            summary=reply.summary,
            next_step=reply.next_step,
            revision=revision,
            unapplied=reply.unapplied,
        )
    )
    work.carried = result
    if reply.outcome is HypothesisOutcome.IMPLEMENTATION_FAILED:
        failed = Blocker(
            kind=BlockerKind.FAILED,
            summary=reply.summary,
            next_step=reply.next_step,
            revision=revision,
        )
        return Unmeasured(blocker=failed, result=result)
    if stray := _stray(w, reply):
        rejected = Blocker(kind=BlockerKind.REPLY_REJECTED, revision=revision, stray=stray)
        return Unmeasured(blocker=rejected, result=result)
    if reply.produces_revision() and (revision is None or revision == w.parent):
        raise WorkEndedError(Failure(kind=FailureKind.NO_CHANGED_CANDIDATE))
    return result


def _stray(w: WorkstreamStart, reply: ImplementerResult) -> tuple[StrayUnapplied, ...]:
    """The ``unapplied`` entries that name no part of the combine (any entry, outside one).

    Such an entry is no fact about this candidate, and the implementer's report that a part was
    dropped would be lost, so the reply is refused with the entries named, not trimmed.
    """
    parts = {item.hypothesis_id for item in w.combine}
    return tuple(
        StrayUnapplied(index=index, hypothesis_id=item.hypothesis_id)
        for index, item in enumerate(reply.unapplied)
        if item.hypothesis_id not in parts
    )


def _review_due(w: WorkstreamStart, result: Result) -> bool:
    config = w.config
    hypothesis = HypothesisConfig(
        max_rounds=config.start_budget,
        judge_every=config.judge_every,
        max_retries_per_round=max(1, w.turns),
        max_continuation_rounds=config.max_continuation_rounds,
    )
    if result.candidate.outcome is HypothesisOutcome.BLOCKED:
        return False
    return w.review_always or review_due(
        hypothesis, round_number=w.sequence, outcome=result.candidate.outcome
    )


async def review(  # noqa: PLR0913  # LW-220432 [PLR0913]; each argument is a distinct fact of the review (the flow, what is reviewed, what it carries and who judges); bundling them in a throwaway record only renames the parameters.
    ctx: Flow,
    w: WorkstreamStart,
    ws: Workspace,
    work: Work,
    judge: Session,
    result: Result,
) -> Result | Unmeasured:
    """Have the judge grade the candidate, showing it what the author's own evaluations said."""
    revision = result.candidate.revision
    if revision is None:
        message = "only a changed candidate is reviewed"
        raise ValueError(message)
    refs = ctx.evaluation.taken(ws, revision)
    readings = await decode_evaluations(ctx, ws, refs) if refs else ()
    prompt = review_prompt(
        w.plan, revision, result.candidate.summary, review_evaluations(refs, readings)
    )
    match await judge.turn(prompt, revision=revision, label=turn_label(judge)):
        case TurnFinished(reply=ReviewResult() as reply, invocation=invocation):
            graded = result.model_copy(
                update={"verdict": Verdict(passed=reply.passed, invocation=invocation)}
            )
            if reply.passed:
                work.carried = graded
                return graded
            rejected = Blocker(
                kind=BlockerKind.REJECTED,
                summary=reply.feedback or reply.analysis,
                revision=revision,
            )
            return Unmeasured(blocker=rejected, result=graded)
        case Failed() as failed:
            raise ended_by(failed, AgentName.JUDGE, plan=w.plan)
        case TurnFinished() as unexpected:
            message = f"the judge answered with {type(unexpected.reply).__name__}"
            raise TypeError(message)


async def decode_evaluations(
    ctx: Flow, ws: Workspace, refs: tuple[EvidenceRef, ...]
) -> tuple[AcceptedReading, ...]:
    """What the evidence owner decoded of the author's own evaluations; none if it could not."""
    seconds = ctx.task.terms.operation_seconds
    outcome = await ctx.evaluation.readings(refs, workspace=ws, within=seconds)
    if isinstance(outcome, Failed):
        raise ended_by(outcome, None)
    if not isinstance(outcome, EvidenceReadings) or outcome.status != "succeeded":
        return ()
    accepted = accept_readings(outcome.readings, refs)
    return () if isinstance(accepted, str) else accepted
