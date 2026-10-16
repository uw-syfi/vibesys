"""The labels the serial searches give their spans, in the shared vocabulary (``search_spans``).

A serial search plays one round at a time, and a round works on one hypothesis in one workspace,
so each round's task is a workstream: it is labelled as the round begins, named for its
hypothesis once the round knows it (after the designer, for a new one), linked to the workstream
that kept the revision it builds on, and accepted or rejected by how its workspace settled. Its
turns are agent turns (``turn_label``) and the trusted measurements it takes are its gates
(``measure_gates``). Every label goes through ``ctx.label``; nothing here builds a projection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.search_spans.api import (
    ACCEPTED,
    BUILDS_ON,
    REJECTED,
    WORKSTREAM,
    WorkstreamAttrs,
)
from vs_core.api.search import SpanLink

if TYPE_CHECKING:
    from vibesys.hypothesis import OrchestratorPlan
    from vibesys.serial_search._run import RoundStart
    from vs_core.api.search import Flow, RevisionRef, Settlement

NO_DESIGN = "no_design"
"""Why a round that designed no hypothesis is rejected."""
NOT_OPENED = "not_opened"
"""Why a round whose workspace never opened is rejected."""


def round_begun(ctx: Flow, start: RoundStart) -> None:
    """Label the round's span a workstream before it knows its hypothesis."""
    ctx.label(WORKSTREAM, title=f"round {start.number}", attrs=WorkstreamAttrs(round=start.number))


def hypothesis_chosen(
    ctx: Flow, start: RoundStart, plan: OrchestratorPlan, base: RevisionRef
) -> None:
    """Name the round's workstream for its hypothesis and link it to the one that kept ``base``."""
    built = start.kept_by(base)
    ctx.label(
        WORKSTREAM,
        title=plan.title or plan.hypothesis_id,
        attrs=WorkstreamAttrs(
            hypothesis_id=plan.hypothesis_id,
            parent_revision=base.revision_id.root,
            round=start.number,
        ),
        links=() if built is None else (SpanLink(rel=BUILDS_ON, to=built),),
    )


def round_settled(ctx: Flow, settlement: Settlement | None) -> None:
    """Accept the round's workstream when its workspace kept what it made, else reject it.

    A workspace keeps a candidate or work in progress later rounds build on; one that discards,
    or never opened, rejects the workstream.
    """
    if settlement is None:
        ctx.label(WORKSTREAM, verdict=REJECTED, reason=NOT_OPENED)
        return
    kept = settlement.retention != "discard"
    ctx.label(WORKSTREAM, verdict=ACCEPTED if kept else REJECTED)


def not_designed(ctx: Flow) -> None:
    """Reject the round's workstream: the designer gave it no hypothesis."""
    ctx.label(WORKSTREAM, verdict=REJECTED, reason=NO_DESIGN)
