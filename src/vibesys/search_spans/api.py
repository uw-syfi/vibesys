"""The kinds of span the hypothesis searches label, shared so every search speaks one vocabulary.

The runtime records what ran; these say what it means, the same way for every search: a planning
call, a workstream (which hypothesis, built on which revision, accepted or rejected, linked to the
workstream it builds on), an agent turn (which role and model) and a gate (a trusted measurement:
its stages, the candidate's value and how it compares with the input, and whether it passed).
Clients read these kinds (``@vibesys/run-state/overview``) and word them; a search declares
``SPANS`` at ``@search(spans=...)`` and labels through ``ctx.label`` or ``label=``. How a search
maps its own steps onto these kinds is its own (the dynamic search's ``_spans``, the serial
searches' ``serial_search``).
"""

from typing import Literal, Self

from vs_core.api.search import Label, Session, SpanKind, Value


class PlanningAttrs(Value):
    """A planning call: which call of the run it is."""

    call: int


class WorkstreamAttrs(Value):
    """A workstream: the hypothesis it works on, the revision it builds on, and its round.

    ``round`` is the round that runs it, for a search that plays rounds (the serial searches);
    None for one that plans calls instead. A round labels its workstream as it begins, before
    it has chosen its hypothesis: until then ``hypothesis_id`` and ``parent_revision`` are None.
    """

    hypothesis_id: str | None = None
    parent_revision: str | None = None
    round: int | None = None


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

    @classmethod
    def measured(cls, stage: str, value: float | None, input_value: float | None) -> Self:
        """The gate of ``stage`` that read ``value``, against the input's ``input_value``."""
        ratio = None if value is None or not input_value else value / input_value
        return cls(stage=stage, metric_value=value, vs_baseline=ratio)


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
"""What a hypothesis search declares at ``@search(spans=...)``."""


def turn_label(session: Session) -> Label:
    """The label of each provider turn a session's ask makes: its role and model."""
    role = session.role
    return Label(
        AGENT_TURN, title=role.name, attrs=AgentTurnAttrs(role=role.name, model=role.model)
    )


__all__ = [
    "ACCEPTED",
    "AGENT_TURN",
    "BUILDS_ON",
    "FAILED_CHECK",
    "GATE",
    "PASSED",
    "PLANNING_CALL",
    "REJECTED",
    "SLOWER",
    "SPANS",
    "WORKSTREAM",
    "AgentTurnAttrs",
    "GateAttrs",
    "PlanningAttrs",
    "WorkstreamAttrs",
    "turn_label",
]
