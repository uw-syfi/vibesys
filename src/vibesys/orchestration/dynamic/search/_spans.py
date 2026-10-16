"""The labels the dynamic search gives its spans, in the shared vocabulary (``search_spans``).

A planning call is a task of the root, a workstream is a task the root starts for a plan, each
provider turn is an agent turn and a workstream's trusted measurement is its gate. Every label
goes through ``ctx.label`` or ``label=``; nothing here builds a projection of its own.
"""

from vibesys.orchestration.dynamic.search._outcomes import (
    Closing,
    Work,
    WorkstreamDone,
    WorkstreamStart,
    beats_input,
    headline,
    input_headline,
)
from vibesys.orchestration.dynamic.search._values import WorkKind
from vibesys.search_spans.api import (
    ACCEPTED,
    BUILDS_ON,
    FAILED_CHECK,
    GATE,
    PASSED,
    REJECTED,
    SLOWER,
    WORKSTREAM,
    GateAttrs,
    WorkstreamAttrs,
)
from vs_core.api.search import Flow, Settle, SpanId, SpanLink, Task


def workstream_title(w: WorkstreamStart) -> str:
    """What a workstream is called: its plan's title, else its work id."""
    return w.plan.title or w.plan.work_id


def workstream_attrs(w: WorkstreamStart) -> WorkstreamAttrs:
    """The hypothesis a workstream works on and the revision it builds on."""
    return WorkstreamAttrs(hypothesis_id=w.plan.work_id, parent_revision=w.parent.revision_id.root)


class BuildsOn:
    """The root's links from each workstream to the latest one of the hypothesis it builds on."""

    def __init__(self) -> None:
        """Start with no workstream started."""
        self._latest: dict[str, SpanId] = {}

    def started(self, ctx: Flow, w: WorkstreamStart, task: Task[WorkstreamDone]) -> None:
        """Link the workstream ``task`` runs to its parent hypothesis's latest workstream."""
        span = task.span
        if span is None:
            return
        parent = self._latest.get(w.plan.parent_hypothesis_id or "")
        if parent is not None:
            ctx.label(WORKSTREAM, span=span, links=(SpanLink(rel=BUILDS_ON, to=parent),))
        self._latest[w.plan.work_id] = span


def label_outcome(
    ctx: Flow, w: WorkstreamStart, closing: Closing, settled: Settle, work: Work
) -> None:
    """Label how the workstream ended, and what its gate measured, once its settlement is asked.

    A workstream is accepted when its workspace keeps what it made (a candidate, or work in
    progress later steps build on); a profile only looks, so it reaches no verdict. Its gate
    passed when the candidate is eligible; else it is slower when it does not beat the input,
    and failed a check otherwise.
    """
    profile = w.plan.kind is WorkKind.PROFILE
    if not profile:
        kept = settled.retention != "discard"
        reason = None if closing.cause is None else closing.cause.kind.value
        ctx.label(WORKSTREAM, verdict=ACCEPTED if kept else REJECTED, reason=reason)
    if work.gate is None:
        return
    span, stage = work.gate
    value, base = headline(closing), input_headline(work.baseline)
    attrs = GateAttrs.measured(
        stage, None if value is None else value.value, None if base is None else base.value
    )
    if profile:
        ctx.label(GATE, span=span, attrs=attrs)
        return
    if settled.eligible:
        verdict = PASSED
    elif not beats_input(closing, ctx.task.terms, work.baseline):
        verdict = SLOWER
    else:
        verdict = FAILED_CHECK
    ctx.label(GATE, span=span, attrs=attrs, verdict=verdict)
