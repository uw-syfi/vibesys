"""The work of a workstream in its open workspace: implement and measure, or profile.

Each piece of work checkpoints what it established (`_resume.mark`), and a workstream resumed
after a code change goes on from its checkpoint (``at``) rather than doing it again.
"""

from vibesys.hypothesis import HypothesisOutcome
from vibesys.orchestration.dynamic.models import MergeOutcomeKind, MergeResult
from vibesys.orchestration.dynamic.search._briefs import merge_prompt, profile_prompt
from vibesys.orchestration.dynamic.search._implement import (
    Conversation,
    implement_until_measurable,
)
from vibesys.orchestration.dynamic.search._measure import evidence_of, measurement_of
from vibesys.orchestration.dynamic.search._outcomes import (
    Closing,
    Evidence,
    MergeReport,
    Result,
    Work,
    WorkEndedError,
    WorkstreamStart,
    ended_by,
)
from vibesys.orchestration.dynamic.search._reports import WorkstreamParking
from vibesys.orchestration.dynamic.search._resume import ResumeStage, WorkstreamResume, mark
from vibesys.orchestration.dynamic.search._roles import IMPLEMENTER, JUDGE, MERGER, PROFILER
from vibesys.orchestration.dynamic.search._values import (
    AgentName,
    Failure,
    FailureKind,
    MergeOutcome,
    WorkKind,
)
from vibesys.search_spans.api import turn_label
from vs_core.api.search import Declined, Failed, Flow, RevisionRef, TurnFinished, Workspace

_IMPLEMENTING = (ResumeStage.WORKING, ResumeStage.IMPLEMENTED)


async def do_work(
    ctx: Flow, w: WorkstreamStart, ws: Workspace, work: Work, at: WorkstreamResume | None
) -> Closing:
    """Let the role this workstream is for do its work; a work that cannot go on closes as failed."""
    try:
        if w.plan.kind is WorkKind.PROFILE:
            return await profile(ctx, w, ws, work, at)
        if w.plan.kind is WorkKind.MERGE:
            return await merge(ctx, w, ws, work, at)
        return await implement_and_measure(ctx, w, ws, work, at)
    except WorkEndedError as ended:
        return work.ended(ended)


async def implement_and_measure(
    ctx: Flow, w: WorkstreamStart, ws: Workspace, work: Work, at: WorkstreamResume | None
) -> Closing:
    """Have the implementer make a candidate, then measure it with the trusted evaluations.

    A resumed implementer works on from its latest candidate.
    """
    if at is None or at.at(*_IMPLEMENTING):
        latest = None if work.carried is None else work.carried.candidate.revision
        implementer = ctx.agents.session(
            IMPLEMENTER, workspace=ws, revision=latest or w.parent, started_by=w.started_by
        )
        # the implementer's candidate is what the judge reviews: its work started the judge
        judge = ctx.agents.session(JUDGE, workspace=ws, started_by=(implementer.agent,))
        parking = WorkstreamParking(ctx, w.key)
        sessions = Conversation(implementer=implementer, judge=judge, parking=parking)
        result = await implement_until_measurable(ctx, w, ws, work, sessions, at)
        mark(ctx, w, work, ResumeStage.READY, result=result)
    elif at.result is not None:
        result = at.result
    else:
        message = f"a workstream checkpointed at {at.stage.value} without its candidate"
        raise ValueError(message)
    if (
        result.candidate.outcome is HypothesisOutcome.BLOCKED
        or not ctx.task.terms.measured
        or not w.plan.measured
    ):
        # nothing was made, nothing is configured to measure, or a plan step is accepted on
        # the judge's review of its own checks
        return Closing(result=result)
    revision = result.candidate.revision or w.parent
    evidence = await measured(ctx, w, ws, work, revision, at, result)
    return Closing(result=result, evidence=evidence)


async def profile(
    ctx: Flow, w: WorkstreamStart, ws: Workspace, work: Work, at: WorkstreamResume | None
) -> Closing:
    """Have the profiler look at the parent, then capture a trusted profile of it.

    What the profiler reports is never read; only what the trusted capture measures is.
    """
    if at is None or at.at(*_IMPLEMENTING):
        profiler = ctx.agents.session(
            PROFILER, workspace=ws, revision=w.parent, started_by=w.started_by
        )
        asked = profile_prompt(w.plan, w.parent)
        if isinstance(failed := await profiler.turn(asked, label=turn_label(profiler)), Failed):
            raise ended_by(failed, AgentName.PROFILER, plan=w.plan)
        mark(ctx, w, work, ResumeStage.READY)
    return Closing(evidence=await measured(ctx, w, ws, work, w.parent, at, None))


async def merge(
    ctx: Flow, w: WorkstreamStart, ws: Workspace, work: Work, at: WorkstreamResume | None
) -> Closing:
    """Have a new merger session merge the candidates on its checkout of main, then measure it.

    The merger takes one turn. Its commit is measured with the trusted evaluations, like an
    implementer's candidate; it becomes main once the workspace settles with it retained. A
    refusal leaves main as it is and carries the merger's reason. A merge resumed after its
    merger committed measures that commit without merging again.
    """
    if at is None or at.at(*_IMPLEMENTING):
        merger = ctx.agents.session(
            MERGER, workspace=ws, revision=w.parent, started_by=w.started_by
        )
        asked = merge_prompt(w.plan, w.parent, w.merge)
        match await merger.turn(asked, label=turn_label(merger)):
            case TurnFinished(reply=MergeResult() as reply, checkpoint=checkpoint):
                report = _merged(w, reply, checkpoint)
            case Failed() as failed:
                raise ended_by(failed, AgentName.MERGER, plan=w.plan)
            case TurnFinished() as unexpected:
                message = f"the merger answered with {type(unexpected.reply).__name__}"
                raise TypeError(message)
        if report.outcome is MergeOutcome.REFUSED:
            return Closing(merge=report, cause=Failure(kind=FailureKind.MERGE_REFUSED))
        work.merged = report
        mark(ctx, w, work, ResumeStage.READY)
    elif work.merged is not None:
        report = work.merged
    else:
        message = f"a merge checkpointed at {at.stage.value} without the merger's commit"
        raise ValueError(message)
    if report.revision is None or not ctx.task.terms.measured:
        return Closing(merge=report)
    evidence = await measured(ctx, w, ws, work, report.revision, at, None)
    return Closing(merge=report, evidence=evidence)


def _merged(w: WorkstreamStart, reply: MergeResult, checkpoint: object) -> MergeReport:
    """What the merger answered: its commit, or its refusal with the reason."""
    if reply.outcome is MergeOutcomeKind.REFUSED:
        return MergeReport(outcome=MergeOutcome.REFUSED, summary=reply.summary, reason=reply.reason)
    if isinstance(checkpoint, Declined):
        raise WorkEndedError(
            Failure(kind=FailureKind.CHECKPOINT_DECLINED, declined=checkpoint.reason)
        )
    revision = getattr(checkpoint, "revision", None)
    if revision is None or revision == w.parent:
        raise WorkEndedError(Failure(kind=FailureKind.NO_CHANGED_CANDIDATE))
    return MergeReport(outcome=MergeOutcome.MERGED, summary=reply.summary, revision=revision)


async def measured(  # noqa: PLR0913  # lint-waiver: LW-271017 [PLR0913]; each argument is a distinct fact of the measurement (the flow, the work, what is measured, and where it resumes).
    # > Bundling them in a record only renames the parameters; the workstream already holds each.
    ctx: Flow,
    w: WorkstreamStart,
    ws: Workspace,
    work: Work,
    revision: RevisionRef,
    at: WorkstreamResume | None,
    result: Result | None,
) -> Evidence:
    """The trusted evidence of ``revision``: measured, checkpointed, then interpreted.

    A workstream resumed with its measurement in hand interprets it without measuring again.
    """
    if at is not None and at.at(ResumeStage.MEASURED) and at.measurement is not None:
        found = at.measurement
    else:
        found = await measurement_of(ctx, w, ws, work, revision)
        mark(ctx, w, work, ResumeStage.MEASURED, result=result, measurement=found)
    return await evidence_of(ctx, w, ws, revision, found)
