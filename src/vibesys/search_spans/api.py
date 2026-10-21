"""The kinds of span the hypothesis searches label, shared so every search speaks one vocabulary.

The runtime records what ran; these say what it means, the same way for every search: a planning
call, a workstream (which hypothesis, based on which revision, passed or failed, linked to the
workstream it is based on), an agent turn (which role and model) and a gate (a trusted measurement:
its stages, the candidate's value and how it compares with the input, and whether it passed) and
the adoption (the candidate the run adopts, what it measured and its ratio to the input's).
Clients read these kinds (``@vibesys/run-state/overview`` and ``/candidates``) and word them.

The candidates of a run are read from its workstreams, the same way for every search: a
workstream is one attempt at a candidate, attempts at one idea share its ``hypothesis_id``, the
``builds_on`` link names the workstream (so the candidate) an attempt is based on (none: the
input), the verdict says whether the idea passed or failed (its own result), ``revision`` names
the code it kept (which the adoption names when it adopts it as the best at the run's end),
``idea`` is what the candidate tries in an agent's words, and the gates under it are what it
measured. A workstream that only looks (``probe``) is not a candidate. A search that plays one
path (a chain of rounds) links each round to the round whose code it starts from (its base), so
its tree is a chain with failed leaves. A search declares
``SPANS`` at ``@search(spans=...)`` and labels through ``ctx.label`` or ``label=``. How a search
maps its own steps onto these kinds is its own (the dynamic search's ``_spans``, the serial
searches' ``serial_search``).
"""

from collections.abc import Sequence
from typing import Literal, Self

from pydantic import Field

from vibesys.metrics import EvidenceReading, MetricRow, reading_of
from vs_core.api.search import (
    ADOPTED,
    NOT_ADOPTED,
    EvidenceKind,
    Input,
    Kept,
    Label,
    Session,
    SpanKind,
    Value,
)


class PlanningAttrs(Value):
    """A planning call: which call of the run it is."""

    call: int


class WorkstreamAttrs(Value):
    """A workstream: one attempt at a candidate, the base it starts from, and what it kept.

    ``hypothesis_id`` is the candidate's identity: every attempt at one idea carries the same
    one. ``round`` is the round that runs it, for a search that plays rounds (the serial
    searches); None for one that plans calls instead. A round labels its workstream as it
    begins, before it has chosen its hypothesis: until then ``hypothesis_id``,
    ``parent_revision`` and ``idea`` are None.

    ``parent_revision`` is the base: the revision the attempt starts from. ``base_round`` is the
    round of the idea that made it (a serial search); None when the base is the input.

    ``idea`` is what the candidate tries, in the words of the agent that chose it (a planner's
    or designer's title, a mutator's summary, an issue's title); None until an agent said it.
    ``revision`` is the revision the attempt kept, set with its verdict; None while it works and
    when it kept nothing. ``outcome`` and ``summary`` are the implementer's own account of the
    attempt (its structured outcome and what it did), set when the attempt settles; None while
    it works and for a search whose agents give none. A ``probe`` only looks (a profile, a
    performance evaluation): it writes no candidate and reaches no verdict, so it is not a
    candidate.
    """

    hypothesis_id: str | None = None
    parent_revision: str | None = None
    base_round: int | None = None
    round: int | None = None
    idea: str | None = None
    revision: str | None = None
    outcome: str | None = None
    summary: str | None = None
    probe: bool = False
    sequence: int | None = Field(default=None, exclude_if=lambda value: value is None)
    """The dynamic workstream admission ordinal, used to join its projected result."""


class FailedCheck(Value):
    """The trusted check that rejected a candidate, and how far it came when it reported that.

    ``completed`` of ``required`` ``unit`` (``93`` of ``100`` ``trials``) is what the check's
    evaluator reported it got through; each is None when it reported no progress.
    """

    check: Literal[EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK]
    completed: int | None = None
    required: int | None = None
    unit: str | None = None

    @classmethod
    def of(cls, readings: Sequence[EvidenceReading]) -> Self | None:
        """The first trusted check (accuracy, then benchmark) that rejected, or None."""
        for kind in (EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK):
            reading = reading_of(tuple(readings), kind)
            if reading is None or reading.passed:
                continue
            partial = reading.partial
            if partial is None:
                return cls(check=kind)
            return cls(
                check=kind,
                completed=partial.completed,
                required=partial.required,
                unit=partial.progress_unit,
            )
        return None


class AgentTurnAttrs(Value):
    """An agent turn: the agent asked, its role, the model it runs on, and who started it.

    ``agent`` is the agent's stable identity (the conversation its turns continue): every turn
    of one agent carries the same, so a client counts an agent's turns by it. ``started_by``
    names the agents whose work started this one (more than one when it serves several, say a
    merge of their work); empty when the search itself started it. ``model`` is None for the
    run's default. The provider and the reasoning effort are the host's settings, which the
    search never sees.
    """

    agent: str
    role: str
    model: str | None = None
    started_by: tuple[str, ...] = ()


class GateAttrs(Value):
    """A trusted measurement of a candidate: its stages, its value, and the ratio to the input's.

    ``failed`` is the first trusted check that rejected the candidate, once its readings are
    read; None when every check it read passed. The verdict (``failed_check`` for a check, or a
    rejection for another reason) is the search's own.
    """

    stage: str
    metric_value: float | None = None
    vs_baseline: float | None = None
    """The candidate's headline value divided by the input's; None while either is unknown."""
    input_metrics: tuple[MetricRow, ...] = ()
    """Every reading of the input's own measurement, headline first: where the input stands on each
    axis, for a client that marks it beside the candidates; empty while it is unmeasured."""
    metrics: tuple[MetricRow, ...] = ()
    """Every reading the benchmark reported, headline first, each with its unit and direction.

    ``metric_value`` is the first of them. A task with several objectives is measured on one row
    each, so a client plots a candidate on every axis from this and never from the headline alone.
    """
    failed: FailedCheck | None = None

    @classmethod
    def read(
        cls,
        stage: str,
        readings: Sequence[EvidenceReading],
        input_metrics: Sequence[MetricRow] = (),
    ) -> Self:
        """The gate of ``stage`` that read ``readings``, against the input's ``input_metrics``.

        Its value is the benchmark's headline and ``metrics`` all its readings; ``failed`` the first
        check that rejected.
        """
        benchmark = reading_of(tuple(readings), EvidenceKind.BENCHMARK)
        headline = None if benchmark is None else benchmark.headline()
        value = None if headline is None else headline.value
        input_value = input_metrics[0].value if input_metrics else None
        ratio = None if value is None or not input_value else value / input_value
        return cls(
            stage=stage,
            metric_value=value,
            vs_baseline=ratio,
            input_metrics=tuple(input_metrics),
            metrics=() if benchmark is None else benchmark.metrics,
            failed=FailedCheck.of(readings),
        )


class AdoptionAttrs(Value):
    """The adoption of the run's winner: the candidate, what it measured and how it compares.

    ``candidate`` is the adopted revision; None when the input itself stands. The value is the
    candidate's headline (the input's when it stands) and ``vs_baseline`` its ratio to the
    input's; either is None while it is unknown.
    """

    candidate: str | None = None
    metric_value: float | None = None
    vs_baseline: float | None = None

    @classmethod
    def of(cls, winner: Kept | Input, value: float | None, input_value: float | None) -> Self:
        """The adoption of ``winner``, which measured ``value`` against the input's."""
        found = None if isinstance(winner, Input) else winner.revision.revision_id.root
        ratio = None if value is None or not input_value else value / input_value
        return cls(candidate=found, metric_value=value, vs_baseline=ratio)


PASSED: Literal["passed"] = "passed"
FAILED: Literal["failed"] = "failed"
FAILED_CHECK: Literal["failed_check"] = "failed_check"
SLOWER: Literal["slower"] = "slower"
BUILDS_ON: Literal["builds_on"] = "builds_on"
TURNED_DOWN: Literal["turned_down"] = "turned_down"
"""Why a workstream whose own checks turned its work down failed though its code is kept.

Its review or a trusted gate rejected it, or its implementer gave it up; the workspace may still
keep the code as work in progress its own hypothesis continues from, so the workstream names the
revision.
"""

PLANNING_CALL = SpanKind("planning_call", attrs=PlanningAttrs)
WORKSTREAM = SpanKind(
    "workstream", attrs=WorkstreamAttrs, verdicts=(PASSED, FAILED), links=(BUILDS_ON,)
)
AGENT_TURN = SpanKind("agent_turn", attrs=AgentTurnAttrs)
GATE = SpanKind("gate", attrs=GateAttrs, verdicts=(PASSED, FAILED_CHECK, SLOWER))
ADOPTION = SpanKind("adoption", attrs=AdoptionAttrs, verdicts=(ADOPTED, NOT_ADOPTED))
SPANS = (PLANNING_CALL, WORKSTREAM, AGENT_TURN, GATE, ADOPTION)
"""What a hypothesis search declares at ``@search(spans=...)``."""


def adoption_label(winner: Kept | Input, value: float | None, input_value: float | None) -> Label:
    """The label of the span that adopts ``winner``, for ``ctx.choose(winner, label)``.

    Core adopts the last choice when the run ends, so every search names its leader with this
    label as it goes; the framework gives the span its verdict (adopted, or not adopted).
    """
    attrs = AdoptionAttrs.of(winner, value, input_value)
    return Label(ADOPTION, title=attrs.candidate or "the input", attrs=attrs)


def turn_label(session: Session) -> Label:
    """The label of each provider turn a session's ask makes: its agent, role, model, starters."""
    role = session.role
    attrs = AgentTurnAttrs(
        agent=session.agent, role=role.name, model=role.model, started_by=session.started_by
    )
    return Label(AGENT_TURN, title=role.name, attrs=attrs)


__all__ = [
    "ADOPTION",
    "AGENT_TURN",
    "BUILDS_ON",
    "FAILED",
    "FAILED_CHECK",
    "GATE",
    "PASSED",
    "PLANNING_CALL",
    "SLOWER",
    "SPANS",
    "TURNED_DOWN",
    "WORKSTREAM",
    "AdoptionAttrs",
    "AgentTurnAttrs",
    "FailedCheck",
    "GateAttrs",
    "PlanningAttrs",
    "WorkstreamAttrs",
    "adoption_label",
    "turn_label",
]
