"""The labels the dynamic search gives its spans, in the shared vocabulary (``search_spans``).

A planning call is a task of the root, a workstream is a task the root starts for a plan, each
provider turn is an agent turn and a workstream's trusted measurement is its gate. Every label
goes through ``ctx.label`` or ``label=``; nothing here builds a projection of its own.
"""

from vibesys.candidates.api import input_metrics, turned_down
from vibesys.orchestration.dynamic.search._outcomes import (
    UNSETTLED,
    Closing,
    Work,
    WorkstreamDone,
    WorkstreamStart,
    beats_input,
)
from vibesys.orchestration.dynamic.search._values import WorkKind
from vibesys.search_spans.api import (
    BUILDS_ON,
    FAILED,
    FAILED_CHECK,
    GATE,
    PASSED,
    SLOWER,
    TURNED_DOWN,
    WORKSTREAM,
    GateAttrs,
    WorkstreamAttrs,
)
from vs_core.api.search import Flow, Settle, SpanId, SpanLink, Task


def workstream_title(w: WorkstreamStart) -> str:
    """What a workstream is called: its plan's title, else its work id."""
    return w.plan.title or w.plan.work_id


def workstream_attrs(w: WorkstreamStart) -> WorkstreamAttrs:
    """The hypothesis a workstream works on, the planner's idea and the revision it builds on.

    A profile only looks, so it is a probe, not a candidate.
    """
    return WorkstreamAttrs(
        hypothesis_id=w.plan.work_id,
        sequence=w.sequence,
        parent_revision=w.parent.revision_id.root,
        idea=w.plan.title or w.plan.hypothesis or None,
        probe=w.plan.kind is WorkKind.PROFILE,
    )


class BuildsOn:
    """The root's links from each workstream to the latest one of the hypothesis it descends from.

    The hypothesis is the one the workstream's parent revision was resolved from at scheduling
    (``WorkstreamStart.parent_hypothesis_id``); a workstream built on the run's main has none.
    """

    def __init__(self) -> None:
        """Start with no workstream started."""
        self._latest: dict[str, SpanId] = {}

    def started(self, ctx: Flow, w: WorkstreamStart, task: Task[WorkstreamDone]) -> None:
        """Link the workstream ``task`` runs to its parent hypothesis's latest workstream."""
        span = task.span
        if span is None:
            return
        parent = self._latest.get(w.parent_hypothesis_id or "")
        if parent is not None:
            ctx.label(WORKSTREAM, span=span, links=(SpanLink(rel=BUILDS_ON, to=parent),))
        self._latest[w.plan.work_id] = span


def label_outcome(
    ctx: Flow, w: WorkstreamStart, closing: Closing, settled: Settle, work: Work
) -> None:
    """Label how the workstream ended, and what its gate measured, once its settlement is asked.

    A workstream passed when its workspace keeps what it made (a candidate, or work in
    progress later steps build on) and its own checks did not turn it down (`turned_down`): a
    kept candidate its review rejected, or whose implementer could not implement it, is a
    failed workstream that names the revision kept. A profile only looks, so it reaches no
    verdict. Its gate
    passed when the candidate is eligible; else it is slower when it does not beat the input,
    and failed a check otherwise.
    """
    profile = w.plan.kind is WorkKind.PROFILE
    if not profile:
        kept = settled.retention != "discard"
        reason = None if closing.cause is None else closing.cause.kind.value
        found = closing.candidate if kept else None
        revision = None if found is None else found.revision_id.root
        ours = workstream_attrs(w).model_copy(update={"revision": revision})
        if kept and turned_down(closing, unsettled=UNSETTLED):
            ctx.label(WORKSTREAM, attrs=ours, verdict=FAILED, reason=TURNED_DOWN)
        else:
            verdict = PASSED if kept else FAILED
            ctx.label(WORKSTREAM, attrs=ours, verdict=verdict, reason=reason)
    if work.gate is None:
        return
    span, stage = work.gate
    attrs = GateAttrs.read(stage, closing.readings, input_metrics(work.baseline))
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
