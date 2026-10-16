"""The labels the evolve search gives its spans, in the shared vocabulary (``search_spans``).

A child is a workstream: it is named for its slot and labelled with the revision it builds on
and the round (the cold start or generation) that runs it; it is linked to the workstream that
kept the revision it builds on, and accepted or rejected by how its workspace settled. Its
turns are agent turns (``turn_label``) and its trusted measurement is its gate (`_measure`).
Every label goes through ``ctx.label``; nothing here builds a projection.
"""

from vibesys.orchestration.evolve.search._values import ChildStart, Closing, Failure
from vibesys.search_spans.api import ACCEPTED, BUILDS_ON, REJECTED, WORKSTREAM, WorkstreamAttrs
from vs_core.api.search import Flow, SpanId, SpanLink

NOT_PASSED = "not_passed"
"""Why a child that left no failure is rejected: it did not pass its review or its gates."""


def title_of(start: ChildStart) -> str:
    """What a child is called: its cold start, or its slot in its generation."""
    if start.cold_start:
        return f"cold start {start.slot}"
    return f"generation {start.generation} · child {start.slot}"


def child_begun(ctx: Flow, start: ChildStart) -> None:
    """Label the child's span a workstream, naming the slot and the revision it builds on."""
    attrs = WorkstreamAttrs(
        hypothesis_id=start.key, parent_revision=start.base.revision_id.root, round=start.round
    )
    ctx.label(WORKSTREAM, title=title_of(start), attrs=attrs)


def child_refused(ctx: Flow, failure: Failure) -> None:
    """Reject the child's workstream: it never got a workspace, or its parent did not verify."""
    ctx.label(WORKSTREAM, verdict=REJECTED, reason=failure.kind.value)


def child_settled(ctx: Flow, closing: Closing, *, retained: bool) -> None:
    """Accept the child's workstream when its workspace kept what it made, else reject it.

    A workspace keeps a candidate, or a cold start's work in progress the next one repairs.
    """
    reason = None
    if not retained:
        reason = NOT_PASSED if closing.cause is None else closing.cause.kind.value
    ctx.label(WORKSTREAM, verdict=ACCEPTED if retained else REJECTED, reason=reason)


def builds_on(ctx: Flow, child: SpanId | None, parent: SpanId | None) -> None:
    """Link the workstream ``child`` to the one that kept the revision it builds on."""
    if child is not None and parent is not None:
        ctx.label(WORKSTREAM, span=child, links=(SpanLink(rel=BUILDS_ON, to=parent),))
