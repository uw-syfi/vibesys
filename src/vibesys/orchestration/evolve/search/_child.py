"""One child: a slot's worth of work, from parent verification to settlement.

The story of `child` is the story of the slot: prove the parent, open a workspace, let the agents
and the gates decide what the child is, close the workspace with what was established. The
workspace is a resource, so it always settles: after the work, after a failure that ends it (the
closing carries the typed failure), and after an operator stop, which interrupts the work
wherever it awaits.
"""

from vibesys.orchestration.evolve.search._settlement import assertion
from vibesys.orchestration.evolve.search._spans import child_begun, child_refused, child_settled
from vibesys.orchestration.evolve.search._values import (
    STOPPED,
    ChildDone,
    ChildStart,
    Closing,
    Failure,
    FailureKind,
)
from vibesys.orchestration.evolve.search._work import work
from vs_core.api.search import (
    Assessment,
    Failed,
    Flow,
    NotOpened,
    ParentVerification,
    Stopped,
    Workspace,
    flow,
)


@flow(snapshot=ChildStart)
async def child(ctx: Flow, start: ChildStart) -> ChildDone:
    """Run one scheduled child to the settlement of its workspace."""
    ctx.checkpoint(start)  # restartable from its start: nothing else is needed to redo it
    child_begun(ctx, start)
    opened: Workspace | None = None
    try:
        unverified = await verify_parent(ctx, start)
        if unverified is not None:
            child_refused(ctx, unverified)
            return ChildDone(start, refused=unverified)
        async with ctx.workspaces.open(
            attempt=start.attempt, item=start.item, plan=start.workspace, budget=start.budget
        ) as admitted:
            if isinstance(admitted, NotOpened):
                refusal = Failure(kind=FailureKind.NOT_OPENED, ended=admitted.failed.reason)
                child_refused(ctx, refusal)
                return ChildDone(start, refused=refusal)
            opened = admitted
            closing = await work(ctx, start, admitted)
            keep(ctx, start, admitted, closing)
    except Stopped:
        closing = Closing().ended_by(STOPPED)
    return ChildDone(start, closing, None if opened is None else opened.settlement)


async def verify_parent(ctx: Flow, start: ChildStart) -> Failure | None:
    """A parent other than the input must still reproduce its retained content."""
    if start.cold_start:
        return None  # a cold start repairs its own earlier work in place
    match await ctx.workspaces.verify(start.base, within=ctx.task.terms.operation_seconds):
        case Failed() as failed:
            return Failure(kind=FailureKind.PARENT_UNVERIFIED, ended=failed.reason)
        case ParentVerification(status="succeeded", verified=True):
            return None
        case ParentVerification():
            return Failure(kind=FailureKind.PARENT_UNVERIFIED)


def keep(ctx: Flow, start: ChildStart, ws: Workspace, closing: Closing) -> None:
    """Assert what the child established for the workspace to settle with."""
    proposed = assertion(start, closing, ctx.task.terms)
    child_settled(ctx, start, closing, retained=proposed.retention != "discard")
    kept = Assessment(
        outcome=proposed.outcome,
        assessments=proposed.assessments,
        eligible=proposed.eligible,
        retention=proposed.retention,
    )
    ctx.workspaces.keep(ws, closing.candidate, kept)
