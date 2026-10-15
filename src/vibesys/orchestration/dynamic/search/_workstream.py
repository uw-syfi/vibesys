"""One workstream: a slot's worth of work, from parent verification to settlement.

The story of `workstream` is the story of the slot: prove the parent, open a workspace, let an
agent do the work, close the workspace with what was established. The workspace is a resource,
so it always settles: after the work, after a failure that ends it (the closing carries the
typed failure), and after an operator stop, which interrupts the work wherever it awaits.
"""

from dataclasses import dataclass

from vibesys.candidates.api import Baseline
from vibesys.orchestration.dynamic.search._measure import the_input
from vibesys.orchestration.dynamic.search._outcomes import (
    Closing,
    Work,
    WorkstreamDone,
    WorkstreamStart,
    assertion,
    failure_of,
)
from vibesys.orchestration.dynamic.search._values import Failure, FailureKind
from vibesys.orchestration.dynamic.search._work import do_work
from vs_core.api.search import (
    Assessment,
    Failed,
    Flow,
    NotOpened,
    ParentVerification,
    RevisionRef,
    Stopped,
    Workspace,
    flow,
)


@flow(snapshot=WorkstreamStart)
async def workstream(ctx: Flow, w: WorkstreamStart) -> WorkstreamDone:
    """Run one scheduled workstream to the settlement of its workspace."""
    ctx.checkpoint(w)  # restartable from its start: nothing else is needed to redo it
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
            closing = await do_work(ctx, w, opened, work)
            if closing.scored:
                await the_input(ctx, w, work)  # a candidate is scored against it
            keep(ctx, w, opened, closing, work.baseline)
    except Stopped:
        closing = work.stopped()
    return WorkstreamDone.closed(w, work.workspace, closing)


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
) -> None:
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
