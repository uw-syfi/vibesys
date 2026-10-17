"""The labels the serial searches give their spans, in the shared vocabulary (``search_spans``).

A serial search plays one round at a time, and a round works on one hypothesis in one workspace,
so each round's task is a workstream: it is labelled as the round begins, named for its
hypothesis once the round knows it (after the designer, for a new one), linked to the workstream
that retained the revision it starts from (its base), and passed or failed by how its idea fared.
Its turns are agent turns (``turn_label``) and the trusted measurements it takes are its gates
(``measure_gates``). Every label goes through ``ctx.label``; nothing here builds a projection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vibesys.search_spans.api import (
    BUILDS_ON,
    FAILED,
    PASSED,
    TURNED_DOWN,
    WORKSTREAM,
    WorkstreamAttrs,
)
from vs_core.api.search import SpanLink

if TYPE_CHECKING:
    from vibesys.hypothesis import OrchestratorPlan
    from vibesys.metrics import AcceptedReading
    from vibesys.serial_search._run import RoundStart
    from vs_core.api.search import Flow, RevisionRef, Settlement

NO_DESIGN = "no_design"
"""Why a round that designed no hypothesis failed."""
NOT_OPENED = "not_opened"
"""Why a round whose workspace never opened failed."""
NOT_MEASURED = "not_measured"
"""Why a round whose kept candidate no trusted gate measured has no result: it failed to pass."""


def round_begun(ctx: Flow, start: RoundStart) -> None:
    """Label the round's span a workstream before it knows its hypothesis."""
    ctx.label(WORKSTREAM, title=f"round {start.number}", attrs=WorkstreamAttrs(round=start.number))


def hypothesis_chosen(
    ctx: Flow, start: RoundStart, plan: OrchestratorPlan, base: RevisionRef
) -> None:
    """Name the round's workstream for its hypothesis and link it to the one that made ``base``."""
    made = start.retained_as(base)
    ctx.label(
        WORKSTREAM,
        title=plan.title or plan.hypothesis_id,
        attrs=_chosen(start, plan, base),
        links=() if made is None else (SpanLink(rel=BUILDS_ON, to=made.span),),
    )


class Settling(Protocol):
    """What a round's settled workstream is labelled from, besides its settlement."""

    plan: OrchestratorPlan
    base: RevisionRef
    readings: tuple[AcceptedReading, ...]
    """The trusted gates' readings of the candidate the round keeps; empty when none measured it."""

    def turned_down(self) -> bool:
        """Whether the round's own checks turned its work down (`serial_search.turned_down`)."""
        ...

    def reported(self) -> tuple[str | None, str] | None:
        """The implementer's account: its outcome (None if it states none) and summary; else None."""
        ...


def round_passed(settlement: Settlement | None, attempt: Settling) -> bool:
    """Whether the round's idea passed.

    It kept what it made, the trusted gates measured it, and its own checks did not turn it down.

    An idea no gate measured (its review was deferred or the gates could not run) has no result, so it did not pass. A winner-eligible idea is never turned
    down (the implementer's provisional advice to discard does not overrule the trusted gates).
    """
    if settlement is None or settlement.retention == "discard":
        return False
    return bool(attempt.readings) and (settlement.eligible or not attempt.turned_down())


def round_settled(
    ctx: Flow, start: RoundStart, attempt: Settling, settlement: Settlement | None
) -> None:
    """Pass the round's workstream when its idea passed, else fail it with the reason.

    A workspace keeps a candidate or work in progress later rounds may build on (the revision
    the workstream names); one that discards, or never opened, fails the workstream. So does a
    round its own checks turned down (the reason is ``turned_down``) and one no gate measured
    (``not_measured``), even when its code is kept as work in progress.
    """
    if settlement is None:
        ctx.label(WORKSTREAM, verdict=FAILED, reason=NOT_OPENED)
        return
    kept = settlement.retention != "discard"
    found = settlement.candidate if kept else None
    revision = None if found is None else found.revision_id.root
    account = attempt.reported()
    attrs = _chosen(start, attempt.plan, attempt.base).model_copy(
        update={
            "revision": revision,
            "outcome": None if account is None else account[0],
            "summary": None if account is None else account[1],
        }
    )
    if round_passed(settlement, attempt):
        ctx.label(WORKSTREAM, attrs=attrs, verdict=PASSED)
    elif not kept:
        ctx.label(WORKSTREAM, attrs=attrs, verdict=FAILED)
    elif attempt.turned_down():
        ctx.label(WORKSTREAM, attrs=attrs, verdict=FAILED, reason=TURNED_DOWN)
    else:
        ctx.label(WORKSTREAM, attrs=attrs, verdict=FAILED, reason=NOT_MEASURED)


def _chosen(start: RoundStart, plan: OrchestratorPlan, base: RevisionRef) -> WorkstreamAttrs:
    """A round's workstream once it chose its hypothesis: the designer's idea, based on ``base``."""
    made = start.retained_as(base)
    return WorkstreamAttrs(
        hypothesis_id=plan.hypothesis_id,
        parent_revision=base.revision_id.root,
        base_round=None if made is None else made.round_number,
        round=start.number,
        idea=plan.title or None,
    )


def not_designed(ctx: Flow) -> None:
    """Fail the round's workstream: the designer gave it no hypothesis."""
    ctx.label(WORKSTREAM, verdict=FAILED, reason=NO_DESIGN)
