"""One workstream: a slot's worth of work, from parent verification to settlement.

The story of `workstream` is the story of the slot: prove the parent, open a workspace, let an
agent do the work, close the workspace with what was established. The workspace is a resource,
so it always settles: after the work, after a failure that ends it (the closing carries the
typed failure), and after an operator stop, which interrupts the work wherever it awaits.

Inside the workspace it checkpoints what it establishes (`_resume`), so after a code change it
resumes in the same workspace from there (`resumed`).
"""

from dataclasses import dataclass

from vibesys.candidates.api import Baseline
from vibesys.orchestration.dynamic.search._measure import the_input
from vibesys.orchestration.dynamic.search._outcomes import (
    RESTARTED,
    Closing,
    Work,
    WorkEndedError,
    WorkstreamDone,
    WorkstreamStart,
    assertion,
    failure_of,
)
from vibesys.orchestration.dynamic.search._reports import RESUMES
from vibesys.orchestration.dynamic.search._resume import ResumeStage, WorkstreamResume, mark
from vibesys.orchestration.dynamic.search._spans import (
    label_outcome,
    workstream_attrs,
    workstream_title,
)
from vibesys.orchestration.dynamic.search._values import Failure, FailureKind
from vibesys.orchestration.dynamic.search._work import do_work
from vibesys.search_spans.api import WORKSTREAM
from vs_core.api.search import (
    Assessment,
    Failed,
    Flow,
    NotOpened,
    NotReopened,
    ParentVerification,
    RevisionRef,
    Settle,
    Stopped,
    Workspace,
    flow,
)


@flow(snapshot=WorkstreamStart | WorkstreamResume)
async def workstream(ctx: Flow, given: WorkstreamStart | WorkstreamResume) -> WorkstreamDone:
    """Run one scheduled workstream to the settlement of its workspace."""
    ctx.checkpoint(given)  # before its workspace opens, it is redone from its start
    if isinstance(given, WorkstreamResume):
        return await resumed(ctx, given)
    w = given
    ctx.label(WORKSTREAM, title=workstream_title(w), attrs=workstream_attrs(w))
    work = Work()
    try:
        unverified = await verify_parent(ctx, w)
        if unverified is not None:
            return WorkstreamDone.not_opened(w, unverified.failure, withheld=unverified.withheld)
        async with ctx.workspaces.open(
            attempt=w.attempt, item=w.item, plan=w.workspace, budget=w.budget
        ) as opened:
            if isinstance(opened, NotOpened):
                return WorkstreamDone.not_opened(w, failure_of(opened.failed, None))
            work.workspace = opened
            mark(ctx, w, work, ResumeStage.WORKING)
            closing = await close_with(ctx, w, opened, work, None)
    except Stopped:
        closing = work.stopped()
    return WorkstreamDone.closed(w, work.workspace, closing)


async def resumed(ctx: Flow, at: WorkstreamResume) -> WorkstreamDone:
    """Go on from the workstream's checkpoint in its workspace, after a code change.

    A workspace the restart could not give back was settled by the workstream's salvage, so the
    workstream ends unmeasured with what it had (`WorkstreamDone.lost`). One that waited in a
    suspended turn goes on once the root lends it an agent slot (`RunRecord.held_over`).
    """
    w, work = at.start, at.work()
    try:
        async with ctx.workspaces.reopen(at.workspace) as ws:
            if isinstance(ws, NotReopened):
                return WorkstreamDone.lost(w, at.workspace, at.result or at.carried, at.merged)
            work.workspace = ws
            await ctx.latch(RESUMES.of(f"{w.key}.0"))
            closing = await close_with(ctx, w, ws, work, at)
    except Stopped:
        closing = work.stopped()
    return WorkstreamDone.closed(w, work.workspace, closing)


async def close_with(
    ctx: Flow, w: WorkstreamStart, ws: Workspace, work: Work, at: WorkstreamResume | None
) -> Closing:
    """Do the work in ``ws`` (or go on from ``at``), and record what its close asserts.

    A workspace whose settlement was asked for before a restart only closes: core settles it as
    it was asked, and the workstream ends with what its checkpoint knew.
    """
    if ws.settling is not None:
        if at is not None and at.closing is not None:
            return at.closing
        return work.ended(WorkEndedError(RESTARTED))
    if at is not None and at.at(ResumeStage.SETTLING) and at.closing is not None:
        closing = at.closing
    else:
        closing = await do_work(ctx, w, ws, work, at)
    if closing.scored(w.plan):
        await the_input(ctx, w, work)  # a candidate is scored against it
    settled = keep(ctx, w, ws, closing, work.baseline)
    label_outcome(ctx, w, closing, settled, work)
    mark(ctx, w, work, ResumeStage.SETTLING, result=closing.result, closing=closing)
    return closing


@dataclass(frozen=True)
class Unverified:
    """A parent that cannot be built on, and whether it is never to be offered again."""

    failure: Failure
    withheld: RevisionRef | None = None


async def verify_parent(ctx: Flow, w: WorkstreamStart) -> Unverified | None:
    """A parent other than the input must still reproduce its retained content."""
    if w.parent == ctx.facts.baseline:
        return None
    match await ctx.workspaces.verify(w.parent, within=ctx.task.terms.operation_seconds):
        case Failed() as failed:
            return Unverified(failure_of(failed, None))
        case ParentVerification(status="succeeded", verified=True):
            return None
        case ParentVerification():
            return Unverified(
                Failure(kind=FailureKind.PARENT_UNVERIFIED, revision=w.parent), withheld=w.parent
            )


def keep(
    ctx: Flow, w: WorkstreamStart, ws: Workspace, closing: Closing, baseline: Baseline | None
) -> Settle:
    """Assert what the workstream established for the workspace to settle with.

    If core will not take that assertion, the workspace settles as a plain failure instead.
    """
    proposed = assertion(w, closing, ctx.task.terms, baseline)
    kept = Assessment(
        outcome=proposed.outcome,
        assessments=proposed.assessments,
        eligible=proposed.eligible,
        retention=proposed.retention,
    )
    ctx.workspaces.keep(ws, closing.candidate, kept)
    return proposed
