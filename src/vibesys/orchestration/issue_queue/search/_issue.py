"""One issue job: claim the issue, let the implementer and the judge work it, close or retry it.

The story of `issue_job` is the story of the slot: open a workspace on the tip, then take
attempts until the judge passes one or the issue's attempts are spent, writing each step to the
board. The workspace is a resource, so it always settles: with the judge's vouching when an
attempt closed the issue with a change, as failed when the job could not go on, and as cancelled
when the operator stopped it wherever it awaited.
"""

from dataclasses import dataclass

from vibesys.candidates.api import review_evaluations
from vibesys.metrics import AcceptedReading, accept_readings
from vibesys.orchestration.issue_queue.models import IssueImplementerResult, IssueJudgeResult
from vibesys.orchestration.issue_queue.search import _board as board
from vibesys.orchestration.issue_queue.search import _writes as writes
from vibesys.orchestration.issue_queue.search._operations import EvidenceReadings
from vibesys.orchestration.issue_queue.search._prompts import ImplementPrompt, ReviewPrompt
from vibesys.orchestration.issue_queue.search._replies import Rejection, rejection_of
from vibesys.orchestration.issue_queue.search._roles import IMPLEMENTER, JUDGE
from vibesys.orchestration.issue_queue.search._settle import assessment_of, retained
from vibesys.orchestration.issue_queue.search._spans import (
    issue_begun,
    issue_not_opened,
    issue_settled,
)
from vibesys.orchestration.issue_queue.search._tracker import write
from vibesys.orchestration.issue_queue.search._values import (
    Closing,
    Failure,
    FailureKind,
    IssueJob,
    JobDone,
    JobEndedError,
    failure_of,
)
from vibesys.search_spans.api import turn_label
from vs_core.api.search import (
    Again,
    Declined,
    EvidenceRef,
    Failed,
    Flow,
    GaveUp,
    InvocationRef,
    NotOpened,
    RetryPolicy,
    RevisionRef,
    Session,
    Tries,
    TurnFinished,
    Workspace,
    flow,
)


@flow(snapshot=IssueJob)
async def issue_job(ctx: Flow, job: IssueJob) -> JobDone:
    """Work one issue to the settlement of its workspace."""
    ctx.checkpoint(job)  # restartable from its start: every board write is keyed
    issue_begun(ctx, job)
    async with ctx.workspaces.open(
        attempt=job.attempt, item=job.item, plan=job.workspace, budget=job.budget
    ) as opened:
        if isinstance(opened, NotOpened):
            failure = Failure(kind=FailureKind.NOT_OPENED, reason=opened.failed.reason.value)
            issue_not_opened(ctx, Closing(failure=failure))
            return JobDone(issue_id=job.issue.id, closing=Closing(failure=failure), refunded=True)
        closing = await work_issue(ctx, job, opened)
        issue_settled(ctx, job, closing, retained=retained(closing))
        revision = closing.candidate if retained(closing) else None
        ctx.workspaces.keep(opened, revision, assessment_of(closing))
    return JobDone(
        issue_id=job.issue.id,
        closing=closing,
        kept=opened.kept if isinstance(opened, Workspace) else None,
    )


@dataclass(frozen=True)
class Conversation:
    """The two agents of one issue job: each is one conversation for the whole job."""

    implementer: Session
    judge: Session


async def work_issue(ctx: Flow, job: IssueJob, ws: Workspace) -> Closing:
    """Take attempts at the issue until one closes it or its attempts are spent.

    A job that cannot go on (a failed board write, an agent that gave no reply) closes with the
    failure and settles as failed; the issue is left as the board last recorded it.
    """
    try:
        await write(ctx, job.config, writes.claim(job), "claiming the issue")
        implementer = ctx.agents.session(IMPLEMENTER, workspace=ws, revision=job.base)
        # the implementer's candidate is what the judge reviews: its work started the judge
        judge = ctx.agents.session(JUDGE, workspace=ws, started_by=(implementer.agent,))
        agents = Conversation(implementer=implementer, judge=judge)

        async def attempt(tries: Tries[Rejection]) -> Closing | Again[Rejection]:
            return await take_attempt(ctx, job, ws, agents, tries)

        policy = RetryPolicy(max_tries=job.config.max_attempts_per_issue)
        match await ctx.retry(attempt, policy):
            case Closing() as closing:
                return closing
            case GaveUp():
                await write(ctx, job.config, writes.block(job), "blocking the issue")
                return Closing()
    except JobEndedError as ended:
        return Closing(failure=ended.failure)


async def take_attempt(
    ctx: Flow, job: IssueJob, ws: Workspace, agents: Conversation, tries: Tries[Rejection]
) -> Closing | Again[Rejection]:
    """One attempt: implement, record it, review it and file the verdict."""
    earlier = tries.reasons[-1] if tries.reasons else None
    made = await implement(agents.implementer, job, earlier)
    await write(
        ctx, job.config, writes.record(job, tries.number, made.reply), "recording the attempt"
    )
    judged = await review(ctx, job, ws, agents.judge, made)
    await write(
        ctx, job.config, writes.verdict(job, tries.number, judged.reply), "filing the verdict"
    )
    if judged.reply.verdict == "pass":
        return Closing(closed=True, candidate=made.candidate, judge=judged.invocation)
    return Again(reason=rejection_of(judged.reply))


@dataclass(frozen=True)
class Implemented:
    """An implementer's reply, and the revision core retained for it when it changed anything."""

    reply: IssueImplementerResult
    candidate: RevisionRef | None


async def implement(session: Session, job: IssueJob, rejected: Rejection | None) -> Implemented:
    """One implementer turn: what it reports, and the revision it left."""
    prompt = ImplementPrompt(
        issue=board.detail(job.issue),
        board=job.board,
        feedback=None if rejected is None else rejected.feedback,
        analysis=None if rejected is None else rejected.analysis,
    )
    match await session.turn(prompt, label=turn_label(session)):
        case TurnFinished(reply=IssueImplementerResult() as reply, checkpoint=checkpoint):
            return Implemented(reply=reply, candidate=changed(job.base, checkpoint))
        case Failed() as failed:
            raise JobEndedError(failure_of(failed, IMPLEMENTER.name))
        case TurnFinished() as unexpected:
            message = f"the implementer answered with {type(unexpected.reply).__name__}"
            raise TypeError(message)


def changed(base: RevisionRef, checkpoint: object) -> RevisionRef | None:
    """The revision core retained for the turn, unless the turn left the tree as it found it."""
    if isinstance(checkpoint, Declined):
        failure = Failure(
            kind=FailureKind.CHECKPOINT_DECLINED,
            role=IMPLEMENTER.name,
            reason=checkpoint.reason.value,
        )
        raise JobEndedError(failure)
    revision: RevisionRef | None = getattr(checkpoint, "revision", None)
    return None if revision == base else revision


@dataclass(frozen=True)
class Judged:
    """The judge's verdict, and the turn it came from."""

    reply: IssueJudgeResult
    invocation: InvocationRef


async def review(
    ctx: Flow, job: IssueJob, ws: Workspace, judge: Session, made: Implemented
) -> Judged:
    """Have the judge grade the attempt, showing it what the author's own evaluations said."""
    revision = made.candidate or job.base
    refs = ctx.evaluation.taken(ws, revision)
    readings = await decode_evaluations(ctx, ws, refs) if refs else ()
    prompt = ReviewPrompt(
        issue=board.detail(job.issue),
        board=job.board,
        summary=made.reply.summary,
        files_touched=made.reply.files_touched,
        self_check=made.reply.self_check,
        evaluations=review_evaluations(refs, readings),
    )
    match await judge.turn(prompt, revision=revision, label=turn_label(judge)):
        case TurnFinished(reply=IssueJudgeResult() as reply, invocation=invocation):
            return Judged(reply=reply, invocation=invocation)
        case Failed() as failed:
            raise JobEndedError(failure_of(failed, JUDGE.name))
        case TurnFinished() as unexpected:
            message = f"the judge answered with {type(unexpected.reply).__name__}"
            raise TypeError(message)


async def decode_evaluations(
    ctx: Flow, ws: Workspace, refs: tuple[EvidenceRef, ...]
) -> tuple[AcceptedReading, ...]:
    """What the evidence owner decoded of the author's own evaluations; none if it could not.

    The judge is shown a record whose reading is unavailable as "result not available", so a
    failure to decode costs the review a detail, not the job.
    """
    seconds = ctx.task.terms.operation_seconds
    outcome = await ctx.evaluation.readings(refs, workspace=ws, within=seconds)
    if isinstance(outcome, Failed) or not isinstance(outcome, EvidenceReadings):
        return ()
    if outcome.status != "succeeded":
        return ()
    accepted = accept_readings(outcome.readings, refs)
    return () if isinstance(accepted, str) else accepted
