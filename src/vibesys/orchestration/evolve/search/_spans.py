"""The labels the evolve search gives its spans, in the shared vocabulary (``search_spans``).

A child is a workstream: it is named for its slot and labelled with the revision it builds on
and the round (the cold start or generation) that runs it, and its idea is the mutator's summary
once the mutator gave one; it is linked to the workstream that kept the revision it builds on,
and passed (naming the revision it kept) or failed by how its workspace settled. Its
turns are agent turns (``turn_label``) and its trusted measurement is its gate (`_measure`).
Every label goes through ``ctx.label``; nothing here builds a projection.
"""

from vibesys.candidates.api import turned_down
from vibesys.orchestration.evolve.search._values import ChildStart, Closing, Failure
from vibesys.search_spans.api import (
    BUILDS_ON,
    FAILED,
    PASSED,
    TURNED_DOWN,
    WORKSTREAM,
    WorkstreamAttrs,
)
from vs_core.api.search import Flow, RevisionRef, SpanId, SpanLink

NOT_PASSED = "not_passed"
"""Why a child that left no failure failed: it did not pass its review or its gates."""


def title_of(start: ChildStart) -> str:
    """What a child is called: its cold start, or its slot in its generation."""
    if start.cold_start:
        return f"cold start {start.slot}"
    return f"generation {start.generation} · child {start.slot}"


def _attrs(start: ChildStart, idea: str = "", kept: RevisionRef | None = None) -> WorkstreamAttrs:
    """The child's slot and base, with the mutator's idea and the kept revision once known."""
    return WorkstreamAttrs(
        hypothesis_id=start.key,
        parent_revision=start.base.revision_id.root,
        round=start.round,
        idea=idea or None,
        revision=None if kept is None else kept.revision_id.root,
    )


def child_begun(ctx: Flow, start: ChildStart) -> None:
    """Label the child's span a workstream, naming the slot and the revision it builds on."""
    ctx.label(WORKSTREAM, title=title_of(start), attrs=_attrs(start))


def mutated(ctx: Flow, start: ChildStart, closing: Closing) -> None:
    """Name the child's idea: what the mutator says it tried."""
    ctx.label(WORKSTREAM, attrs=_attrs(start, closing.summary))


def child_refused(ctx: Flow, failure: Failure) -> None:
    """Reject the child's workstream: it never got a workspace, or its parent did not verify."""
    ctx.label(WORKSTREAM, verdict=FAILED, reason=failure.kind.value)


def child_settled(ctx: Flow, start: ChildStart, closing: Closing, *, retained: bool) -> None:
    """Accept the child's workstream when it kept what it made and succeeded, else reject it.

    A workspace keeps a candidate, or a cold start's work in progress the next one repairs. A
    child its own checks turned down (`turned_down`: its review or a trusted gate rejected it)
    failed even when its code is kept, and then names the revision kept.
    """
    if retained:
        attrs = _attrs(start, closing.summary, closing.candidate)
        if turned_down(closing, unsettled=frozenset()):
            ctx.label(WORKSTREAM, attrs=attrs, verdict=FAILED, reason=TURNED_DOWN)
            return
        ctx.label(WORKSTREAM, attrs=attrs, verdict=PASSED)
        return
    reason = NOT_PASSED if closing.cause is None else closing.cause.kind.value
    attrs = _attrs(start, closing.summary)
    ctx.label(WORKSTREAM, attrs=attrs, verdict=FAILED, reason=reason)


def builds_on(ctx: Flow, child: SpanId | None, parent: SpanId | None) -> None:
    """Link the workstream ``child`` to the one that kept the revision it builds on."""
    if child is not None and parent is not None:
        ctx.label(WORKSTREAM, span=child, links=(SpanLink(rel=BUILDS_ON, to=parent),))
