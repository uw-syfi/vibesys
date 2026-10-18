"""Every way core can end a wait, applied to any search under its own fake core.

A search's host (`tests/vibesys/orchestration/<search>/_host.py`) answers the waits its flow
opens the ordinary way and in the ways that are particular to it (a plan with no hypotheses, a
checkpoint core declined). What core does to *any* wait is the same for every search: it refuses
the decision with any `RejectionCode`, loses it, lets it time out; a turn's transport drops, the
provider fails it, the reply is not JSON or breaks its model, the prompt does not render, the
agent suspends it to wait for an evaluation (where the view offers that) and core resumes it or
refuses its resume for good, or replies with its reply model filled with odd values drawn from
the model's schema; an operation's owner reports a failure; a measurement leaves no evidence;
the adoption of the winner fails. `FAULTS` lists those once, so a new search is held to all of
them by registering its host, not by copying a menu.

`play` runs one generated story: each step picks an open wait and answers it the host's way
(``variant``) or with a fault, the operator may stop (draining or cancelling) before any step, and
then everything is answered the ordinary way. It checks the promises every search makes whatever
core answers: the run ends in exactly one `Finish`, with every attempt it opened settled, no wait
left open, no attempt started after the stop, and any winner it proposed officially measured and
settled as an eligible candidate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from hypothesis import strategies as st
from pydantic import ValidationError

from vs_core.api import (
    AdoptionFailureReason,
    CheckpointDecline,
    Declined,
    FailureReason,
    Finish,
    Measure,
    MeasurementFinished,
    MeasurementResult,
    ObservationStatus,
    Operation,
    ProposeWinner,
    RejectionCode,
    ReplyError,
    ReplyErrorCode,
    RequestTurn,
    ResumeRefusal,
    Retained,
    RetainedCandidate,
    RevisionRef,
    Settle,
    SpanStatus,
    StartAttempt,
    StopMode,
    TurnFailureKind,
    Wait,
    Withdraw,
    operation_span,
)
from vs_sim.api import SeededRandom

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pydantic import BaseModel

    from vs_core.api import AttemptRef
    from vs_core.api.testing import Asked, FlowRun


class SearchHost(Protocol):
    """What `play` needs of a search's host."""

    @property
    def run(self) -> FlowRun[Any]:
        """The flow being played."""
        ...

    @property
    def unsettled(self) -> Sequence[AttemptRef]:
        """Attempts the host opened and did not close."""
        ...

    def answer(self, asked: Asked, variant: int = 0) -> None:
        """Answer one open wait the host's way; ``variant`` chooses among them."""
        ...


@dataclass(frozen=True)
class Fault:
    """One way core ends a wait, and the waits it can end that way."""

    name: str
    applies: Callable[[FlowRun[Any], Asked], bool]
    apply: Callable[[FlowRun[Any], Asked], None]


def _work(run: FlowRun[Any], asked: Asked) -> bool:
    """A wait core may refuse, lose or time out: anything but the close of a workspace.

    Core never refuses the close of an attempt; it refuses a settling close only when the settle
    keeps work (the host then closes it plainly), which the hosts' own variants already play.
    """
    del run
    return not isinstance(asked.request, Withdraw)


def _turn(run: FlowRun[Any], asked: Asked) -> bool:
    del run
    return isinstance(asked.request, RequestTurn)


def _suspendable(run: FlowRun[Any], asked: Asked) -> bool:
    return _turn(run, asked) and "suspend" in run.view.current().capabilities.lifecycle


def _suspended(run: FlowRun[Any], asked: Asked) -> None:
    run.resume_ready(run.suspend(asked))


def _resume_refused(reason: ResumeRefusal) -> Fault:
    """The turn suspended, and the history its evaluations left can never back its resume."""
    return Fault(
        f"resume-refused({reason.value})",
        _suspendable,
        lambda run, asked: run.resume_refused(run.suspend(asked), reason),
    )


def _refused(code: RejectionCode) -> Fault:
    return Fault(
        f"refused({code.value})",
        _work,
        lambda run, asked: run.fail(asked, FailureReason.REFUSED, cause=code),
    )


def _turn_failed(kind: TurnFailureKind) -> Fault:
    return Fault(f"turn-failed({kind.value})", _turn, lambda run, asked: run.fail_turn(asked, kind))


def _raw(text: str) -> Fault:
    return Fault(f"raw({text})", _turn, lambda run, asked: run.reply_raw(asked, text))


def _outcome(asked: Asked, status: str) -> BaseModel | None:
    """The operation's outcome reporting ``status`` and nothing else, if its model allows one."""
    request = asked.request
    if not isinstance(request, Operation):
        return None
    model = getattr(request.request, "outcome_model", None)
    if model is None:
        return None
    try:
        return model.model_validate({"status": status})
    except ValidationError:
        return None


def _operation_ended(status: str) -> Fault:
    """The operation ran and its owner reports ``status``, with nothing else to show for it."""

    def apply(run: FlowRun[Any], asked: Asked) -> None:
        outcome = _outcome(asked, status)
        assert outcome is not None
        run.finish_operation(asked, outcome)

    return Fault(
        f"operation-{status}", lambda _run, asked: _outcome(asked, status) is not None, apply
    )


def _measure(run: FlowRun[Any], asked: Asked) -> bool:
    del run
    return isinstance(asked.request, Measure)


def _unmeasured(run: FlowRun[Any], asked: Asked) -> None:
    """The measurement ended failed without any evidence."""
    request = asked.request
    assert isinstance(request, Measure)
    result = MeasurementResult(scope=request.scope, evidence=(), status=ObservationStatus.FAILED)
    run.answer(asked, MeasurementFinished(decision=asked.decision, result=result))


def _proposal(run: FlowRun[Any], asked: Asked) -> bool:
    del run
    return isinstance(asked.request, ProposeWinner)


def _not_adopted(reason: AdoptionFailureReason) -> Fault:
    return Fault(
        f"not-adopted({reason.value})",
        _proposal,
        lambda run, asked: run.refuse_adoption(asked, reason),
    )


_STRINGS = ("", " ", "x", "h1", "c1", "0", "-1", "nominated", "pass", "../x", "a" * 300, "é\u0000")
_INTS = (0, 1, -1, 2, 3, 1000, 2**31)
_FLOATS = (0.0, 1.0, -1.0, 0.5, 1e9, -1e9)
_MAX_DEPTH = 3

type Schema = dict[str, Any]


def _resolved(schema: Schema, defs: Schema, rng: SeededRandom) -> Schema:
    """The schema with references followed and one branch of each union chosen."""
    while True:
        if "$ref" in schema:
            schema = defs[schema["$ref"].rsplit("/", 1)[1]]
        elif "anyOf" in schema or "oneOf" in schema:
            schema = rng.choice(schema.get("anyOf") or schema["oneOf"])
        elif "allOf" in schema:
            schema = schema["allOf"][0]
        else:
            return schema


def _array(schema: Schema, defs: Schema, rng: SeededRandom, depth: int) -> list[object]:
    if "prefixItems" in schema:
        return [_value(item, defs, rng, depth + 1) for item in schema["prefixItems"]]
    size = 0 if depth > _MAX_DEPTH else rng.choice((0, 1, 1, 2, 3))
    return [_value(schema.get("items") or {}, defs, rng, depth + 1) for _ in range(size)]


def _object(schema: Schema, defs: Schema, rng: SeededRandom, depth: int) -> dict[str, object]:
    required = set(schema.get("required", ()))
    fields = {
        name: _value(sub, defs, rng, depth + 1)
        for name, sub in schema.get("properties", {}).items()
        if name in required or rng.random() < 0.6
    }
    extra = schema.get("additionalProperties")
    if isinstance(extra, dict) and rng.random() < 0.5:
        fields[rng.choice(_STRINGS[:6])] = _value(extra, defs, rng, depth + 1)
    return fields


def _number(schema: Schema, rng: SeededRandom, odd: tuple[float, ...]) -> float:
    """An odd number, or one at an edge of the schema's bounds, often enough to pass them."""
    edges = [
        bound + offset
        for key, inside in (("minimum", 0), ("exclusiveMinimum", 1), ("maximum", 0))
        if isinstance(bound := schema.get(key), int | float)
        for offset in (inside, inside + 1)
    ]
    return rng.choice((*odd, *edges, *edges))


_LEAVES: dict[str, Callable[[Schema, SeededRandom], object]] = {
    "string": lambda _schema, rng: rng.choice(_STRINGS),
    "integer": lambda schema, rng: int(_number(schema, rng, _INTS)),
    "number": lambda schema, rng: _number(schema, rng, _FLOATS),
    "boolean": lambda _schema, rng: rng.random() < 0.5,
    "null": lambda _schema, _rng: None,
}


def _value(schema: Schema, defs: Schema, rng: SeededRandom, depth: int) -> object:
    """A value of ``schema``, chosen among odd ones: empty, long, negative, absent."""
    schema = _resolved(schema, defs, rng)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return rng.choice(schema["enum"])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = rng.choice(kind)
    if kind == "array":
        return _array(schema, defs, rng, depth)
    if kind == "object":
        return _object(schema, defs, rng, depth)
    leaf = _LEAVES.get(str(kind))
    return rng.choice((None, "", 0)) if leaf is None else leaf(schema, rng)


def _odd_reply(model: type[BaseModel], rng: SeededRandom) -> BaseModel | None:
    """A valid ``model`` with odd values, or None when no try of a few validates.

    It validates from JSON, as an agent's reply or an operator's options arrive.
    """
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    for _ in range(40):
        try:
            return model.model_validate_json(json.dumps(_value(schema, defs, rng, 0)))
        except ValidationError:
            continue
    return None


def _odd(run: FlowRun[Any], asked: Asked) -> None:
    """The agent replies with its reply model, valid and filled with odd values; core checkpoints.

    The values come from the model's JSON schema (empty, long, negative, every enum member,
    optional fields left out), seeded by the decision, so a story replays exactly.
    """
    request = asked.request
    assert isinstance(request, RequestTurn)
    model = run.definition.reply_schemas[request.turn.output_schema]
    rng = SeededRandom(f"{asked.decision.root}/{len(run.decisions)}")
    reply = _odd_reply(model, rng)
    if reply is None:
        run.reply_raw(asked, "{}")
        return
    revision = RevisionRef.of_git_commit(f"odd-{len(run.decisions)}")
    checkpoint = (
        Retained(revision=revision)
        if rng.random() < 0.8
        else Declined(reason=rng.choice(list(CheckpointDecline)))
    )
    run.reply(asked, reply, checkpoint=checkpoint)


_BROKEN = (ReplyError(path=("summary",), code=ReplyErrorCode.MISSING),)

FAULTS: tuple[Fault, ...] = (
    *(_refused(code) for code in RejectionCode),
    Fault("lost", _work, lambda run, asked: run.fail(asked, FailureReason.LOST)),
    Fault("timed-out", _work, lambda run, asked: run.fail(asked, FailureReason.TIMED_OUT)),
    Fault("transport-lost", _turn, lambda run, asked: run.lose(asked)),
    *(_turn_failed(kind) for kind in TurnFailureKind),
    _raw("not json"),
    _raw("{}"),
    _raw(json.dumps([1, 2])),
    Fault(
        "invalid-reply",
        _turn,
        lambda run, asked: run.fail(asked, FailureReason.INVALID_REPLY, errors=_BROKEN),
    ),
    Fault("render-failed", _turn, lambda run, asked: run.fail(asked, FailureReason.RENDER_FAILED)),
    Fault("suspended", _suspendable, _suspended),
    *(_resume_refused(reason) for reason in ResumeRefusal),
    Fault("odd-reply", _turn, _odd),
    Fault("odd-reply", _turn, _odd),
    Fault("odd-reply", _turn, _odd),
    *(_operation_ended(status) for status in ("failed", "rejected", "cancelled", "unknown")),
    Fault("unmeasured", _measure, _unmeasured),
    *(_not_adopted(reason) for reason in AdoptionFailureReason),
)
"""Every way core ends a wait that is not particular to a search."""


@dataclass(frozen=True)
class Step:
    """Answer the ``pick``-th open wait the host's ``variant`` way, or with ``fault`` if it applies."""

    pick: int
    variant: int
    fault: int | None


@dataclass(frozen=True)
class Stop:
    """The operator stops before the ``before``-th step, in ``mode``."""

    before: int
    mode: StopMode


MAX_STEPS = 60
DRAIN_LIMIT = 1000
"""Ordinary answers after the story within which a flow must settle."""


def steps() -> st.SearchStrategy[list[Step]]:
    """Stories of up to `MAX_STEPS` steps; about one in three answers with a fault."""
    fault = st.none() | st.none() | st.integers(0, len(FAULTS) - 1)
    return st.lists(
        st.builds(Step, st.integers(0, 7), st.integers(0, 13), fault), max_size=MAX_STEPS
    )


def stops() -> st.SearchStrategy[Stop | None]:
    """No stop, or one before some step (or after the last), draining or cancelling."""
    return st.none() | st.builds(Stop, st.integers(0, MAX_STEPS), st.sampled_from(StopMode))


def step(host: SearchHost, chosen: Step) -> bool:
    """Answer one open wait as ``chosen`` says, or move the clock; False when nothing is open."""
    run = host.run
    answerable = [item for item in run.open if item.until is not None]
    if answerable:
        asked = answerable[chosen.pick % len(answerable)]
        fault = None if chosen.fault is None else FAULTS[chosen.fault]
        if fault is not None and fault.applies(run, asked):
            fault.apply(run, asked)
        elif isinstance(asked.request, ProposeWinner):
            run.adopt(asked)
        else:
            host.answer(asked, chosen.variant)
        return True
    if run.sleeping_until:
        run.advance_to(min(run.sleeping_until))
        return True
    return False


def _starts(run: FlowRun[Any]) -> int:
    return sum(
        isinstance(item, Wait) and isinstance(item.request, StartAttempt) for item in run.decisions
    )


def play(
    host: SearchHost,
    story: Sequence[Step],
    stop: Stop | None = None,
    *,
    check: Callable[[], None] = lambda: None,
    work: Callable[[], object] | None = None,
) -> None:
    """Play ``story``, stopping as ``stop`` says, then drain; check ``check`` after every step.

    ``work`` counts the work the search began (by default, the attempts it started), which must
    not grow after the stop. Asserts the promises every search keeps; a search's own test checks
    the rest after it.
    """
    run = host.run
    begun = work or (lambda: _starts(run))
    stopped_with: object = None
    stopped = False

    def stop_now(mode: StopMode) -> None:
        nonlocal stopped_with, stopped
        run.stop(mode=mode)
        stopped_with, stopped = begun(), True

    for index, chosen in enumerate(story):
        if stop is not None and index == stop.before:
            stop_now(stop.mode)
        check()
        if not step(host, chosen):
            break
    if stop is not None and not stopped:
        stop_now(stop.mode)
    for _ in range(DRAIN_LIMIT):
        check()
        if not step(host, Step(0, 0, None)):
            break
    else:
        message = f"the flow did not settle in {DRAIN_LIMIT} steps: {run.timeline()}"
        raise AssertionError(message)
    assert_flow_closed(host)
    _assert_winner_earned(run)
    _assert_adoption_labelled(run)
    finishes = [item for item in run.decisions if isinstance(item, Finish)]
    assert len(finishes) == 1, f"{len(finishes)} finishes: {run.timeline()}"
    if stopped:
        assert begun() == stopped_with, f"work began after the stop: {run.timeline()}"


def _assert_winner_earned(run: FlowRun[Any]) -> None:
    """A candidate proposed as the winner was officially measured and settled as eligible."""
    asked = [item.request for item in run.decisions if isinstance(item, Wait)]
    eligible = {
        item.disposition.candidate
        for item in asked
        if isinstance(item, Withdraw)
        and isinstance(item.disposition, Settle)
        and item.disposition.eligible
        and item.disposition.retention == "candidate"
    }
    measured = {
        item.plan.candidate
        for item in asked
        if isinstance(item, Measure) and item.plan.purpose == "official"
    }
    for item in asked:
        if isinstance(item, ProposeWinner) and isinstance(item.selection, RetainedCandidate):
            revision = item.selection.revision
            assert revision in eligible, f"won without an eligible settle: {run.timeline()}"
            assert revision in measured, f"won without an official measurement: {run.timeline()}"


def _assert_adoption_labelled(run: FlowRun[Any]) -> None:
    """The span that adopts a winner names the candidate, and its verdict says how it went.

    Whatever core answers (it adopts, refuses, loses the proposal, the operator stops), the
    proposal's span is an adoption of exactly the selection proposed; once core has answered it
    is adopted or not adopted, as the run's own result says.
    """
    trace = run.trace()
    finished = run.finished
    for item in run.decisions:
        if not (isinstance(item, Wait) and isinstance(item.request, ProposeWinner)):
            continue
        proposal = item.request
        span = trace.span(operation_span(proposal.decision_id))
        assert span is not None, run.timeline()
        assert span.label is not None, f"{span.id} is not labelled: {run.timeline()}"
        assert span.label.kind == "adoption", run.timeline()
        attrs = json.loads(span.label.attrs_json or "{}")
        selection = proposal.selection
        candidate = (
            selection.revision.revision_id.root
            if isinstance(selection, RetainedCandidate)
            else None
        )
        assert attrs["candidate"] == candidate, run.timeline()
        if attrs["metric_value"] is not None and attrs["vs_baseline"] is not None:
            assert attrs["vs_baseline"] > 0, run.timeline()
        verdict = span.label.verdict
        assert verdict in {None, "adopted", "not_adopted"}, run.timeline()
        if span.status is SpanStatus.DONE:
            assert verdict is not None, f"{span.id} was answered without a verdict"
        if verdict == "not_adopted":
            assert span.label.reason, run.timeline()
        if finished is not None and finished.result.selection == selection:
            assert verdict == "adopted", run.timeline()


def assert_flow_closed(host: SearchHost) -> None:
    """The flow finished the run and left no attempt open and no wait unanswered."""
    run = host.run
    assert run.finished is not None, run.timeline()
    assert not host.unsettled, run.timeline()
    assert not [item for item in run.open if item.until is not None], run.timeline()


__all__ = [
    "FAULTS",
    "Fault",
    "SearchHost",
    "Step",
    "Stop",
    "assert_flow_closed",
    "play",
    "step",
    "steps",
    "stops",
]
