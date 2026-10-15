"""The work of a workstream in its open workspace: implement and measure, or profile."""

from vibesys.hypothesis import HypothesisOutcome
from vibesys.orchestration.dynamic.models import MergeOutcomeKind, MergeResult
from vibesys.orchestration.dynamic.search._briefs import merge_prompt, profile_prompt
from vibesys.orchestration.dynamic.search._implement import (
    Conversation,
    implement_until_measurable,
)
from vibesys.orchestration.dynamic.search._measure import measure_and_interpret, the_input
from vibesys.orchestration.dynamic.search._outcomes import (
    Closing,
    MergeReport,
    Work,
    WorkEndedError,
    WorkstreamStart,
    ended_by,
)
from vibesys.orchestration.dynamic.search._roles import IMPLEMENTER, JUDGE, MERGER, PROFILER
from vibesys.orchestration.dynamic.search._values import (
    AgentName,
    Failure,
    FailureKind,
    MergeOutcome,
    WorkKind,
)
from vs_core.api.search import Declined, Failed, Flow, TurnFinished, Workspace


async def do_work(ctx: Flow, w: WorkstreamStart, ws: Workspace, work: Work) -> Closing:
    """Let the role this workstream is for do its work; a work that cannot go on closes as failed."""
    try:
        if w.plan.kind is WorkKind.PROFILE:
            return await profile(ctx, w, ws, work)
        if w.plan.kind is WorkKind.MERGE:
            return await merge(ctx, w, ws, work)
        return await implement_and_measure(ctx, w, ws, work)
    except WorkEndedError as ended:
        return work.ended(ended)


async def implement_and_measure(
    ctx: Flow, w: WorkstreamStart, ws: Workspace, work: Work
) -> Closing:
    """Have the implementer make a candidate, then measure it with the trusted evaluations."""
    sessions = Conversation(
        implementer=ctx.agents.session(IMPLEMENTER, workspace=ws, revision=w.parent),
        judge=ctx.agents.session(JUDGE, workspace=ws),
    )
    result = await implement_until_measurable(ctx, w, ws, work, sessions)
    if result.candidate.outcome is HypothesisOutcome.BLOCKED or not ctx.task.terms.measured:
        return Closing(result=result)  # nothing was made, or nothing is configured to measure
    await the_input(ctx, w, work)
    evidence = await measure_and_interpret(ctx, w, ws, result.candidate.revision or w.parent)
    return Closing(result=result, evidence=evidence)


async def profile(ctx: Flow, w: WorkstreamStart, ws: Workspace, work: Work) -> Closing:
    """Have the profiler look at the parent, then capture a trusted profile of it.

    What the profiler reports is never read; only what the trusted capture measures is.
    """
    profiler = ctx.agents.session(PROFILER, workspace=ws, revision=w.parent)
    if isinstance(failed := await profiler.turn(profile_prompt(w.plan, w.parent)), Failed):
        raise ended_by(failed, AgentName.PROFILER, plan=w.plan)
    await the_input(ctx, w, work)
    return Closing(evidence=await measure_and_interpret(ctx, w, ws, w.parent))


async def merge(ctx: Flow, w: WorkstreamStart, ws: Workspace, work: Work) -> Closing:
    """Have a new merger session merge the candidates on its checkout of main, then measure it.

    The merger takes one turn. Its commit is measured with the trusted evaluations, like an
    implementer's candidate; it becomes main once the workspace settles with it retained. A
    refusal leaves main as it is and carries the merger's reason.
    """
    merger = ctx.agents.session(MERGER, workspace=ws, revision=w.parent)
    match await merger.turn(merge_prompt(w.plan, w.parent, w.merge)):
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
    if report.revision is None or not ctx.task.terms.measured:
        return Closing(merge=report)
    await the_input(ctx, w, work)
    evidence = await measure_and_interpret(ctx, w, ws, report.revision)
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
