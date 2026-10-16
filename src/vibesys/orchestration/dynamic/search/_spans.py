"""The kinds of span the dynamic search labels, and the labels it gives them.

The runtime records what ran; these say what it means: a planning call, a workstream (which
hypothesis, built on which revision, accepted or rejected, linked to the workstream it builds
on), an agent turn (which role and model) and a gate (a trusted measurement: its stages, the
candidate's value and how it compares with the input, and whether it passed). Every label goes
through ``ctx.label`` or ``label=``; nothing here builds a projection of its own.
"""

from typing import Literal

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
from vs_core.api.search import (
    Flow,
    Label,
    Session,
    Settle,
    SpanId,
    SpanKind,
    SpanLink,
    Task,
    Value,
)


class PlanningAttrs(Value):
    """A planning call: which call of the run it is."""

    call: int


class WorkstreamAttrs(Value):
    """A workstream: the hypothesis it works on and the revision it builds on."""

    hypothesis_id: str
    parent_revision: str


class AgentTurnAttrs(Value):
    """An agent turn: the role asked and the model it runs on (None: the run's default).

    The provider and the reasoning effort are the host's settings, which the search never sees.
    """

    role: str
    model: str | None = None


class GateAttrs(Value):
    """A trusted measurement of a candidate: its stages, its value, and the ratio to the input's."""

    stage: str
    metric_value: float | None = None
    vs_baseline: float | None = None
    """The candidate's headline value divided by the input's; None while either is unknown."""


ACCEPTED: Literal["accepted"] = "accepted"
REJECTED: Literal["rejected"] = "rejected"
PASSED: Literal["passed"] = "passed"
FAILED_CHECK: Literal["failed_check"] = "failed_check"
SLOWER: Literal["slower"] = "slower"
BUILDS_ON: Literal["builds_on"] = "builds_on"

PLANNING_CALL = SpanKind("planning_call", attrs=PlanningAttrs)
WORKSTREAM = SpanKind(
    "workstream", attrs=WorkstreamAttrs, verdicts=(ACCEPTED, REJECTED), links=(BUILDS_ON,)
)
AGENT_TURN = SpanKind("agent_turn", attrs=AgentTurnAttrs)
GATE = SpanKind("gate", attrs=GateAttrs, verdicts=(PASSED, FAILED_CHECK, SLOWER))
SPANS = (PLANNING_CALL, WORKSTREAM, AGENT_TURN, GATE)
"""What ``@search("dynamic", spans=...)`` declares."""


def turn_label(session: Session) -> Label:
    """The label of each provider turn a session's ask makes."""
    role = session.role
    return Label(
        AGENT_TURN, title=role.name, attrs=AgentTurnAttrs(role=role.name, model=role.model)
    )


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
    ratio = None if value is None or base is None or not base.value else value.value / base.value
    attrs = GateAttrs(
        stage=stage, metric_value=None if value is None else value.value, vs_baseline=ratio
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
