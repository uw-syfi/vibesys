"""The run trace's read model: one ``Span`` per flow task and per awaited operation.

The runtime records spans where it journals the transitions (see ``_trace``); a reader gets
them frozen, as a ``RunTrace``. Times are on the run clock as the journal recorded them, so a
replay yields the same spans, ids and times.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BeforeValidator, Field

from vs_core.types.common import Seconds, Value

type SpanId = Annotated[str, Field(pattern=r"^(flow|op):\S*$")]
"""A span's identity: ``flow:<path>#<n>`` for a flow task, ``op:<decision id>`` for an operation."""


class SpanType(StrEnum):
    """What a span stands for. Closed."""

    FLOW = "flow"
    OPERATION = "operation"


class SpanStatus(StrEnum):
    """Where a span is in its lifecycle. Closed."""

    PENDING = "pending"
    """A flow a code change restarts, until its parent starts its key again."""
    RUNNING = "running"
    """An operation in flight, or a flow waiting on work in progress (an operation or a task)."""
    WAITING = "waiting"
    """A flow waiting only on time, a latch or the stop."""
    DONE = "done"
    FAILED = "failed"
    """Ended with a failure outcome (``failure.kind`` is ``outcome``)."""
    STOPPED = "stopped"
    """Ended by an interruption (``failure.kind`` is ``interruption``)."""


class SpanWaitKind(StrEnum):
    """What a flow waits on. Closed."""

    OPERATION = "operation"
    """An operation it issued (``ref`` is the operation's span), or a turn's own signal."""
    SLOT = "slot"
    """A value another flow fills: a task's result (``ref`` is its span), a group's, a latch's."""
    TIME = "time"
    STOP = "stop"


class WaitingOn(Value):
    """One source a suspended flow waits on, since the run-clock time it began waiting on it."""

    kind: SpanWaitKind
    ref: str
    since: Seconds


class AttemptOutcome(StrEnum):
    """How one try of ``ctx.retry`` ended. Closed."""

    DONE = "done"
    AGAIN = "again"
    GAVE_UP = "gave_up"


class SpanAttempt(Value):
    """One try of ``ctx.retry`` in the flow: try ``n`` of ``of``.

    ``outcome`` is None while the try runs, and stays None when the flow ended inside it.
    """

    n: int
    of: int
    started_at: Seconds
    ended_at: Seconds | None = None
    outcome: AttemptOutcome | None = None
    reason: str | None = None
    """Why it asked to try again: the ``Again`` reason, as text."""


class FailureKind(StrEnum):
    """flows.md's failure kinds a span can end with. Closed.

    A defect crashes the step that raised it, which is never persisted, so no trace holds one.
    """

    OUTCOME = "outcome"
    INTERRUPTION = "interruption"


class SpanFailure(Value):
    """Why a span ended failed or stopped."""

    kind: FailureKind
    message: str


class SpanStop(Value):
    """The operator's stop as this flow saw it: when it arrived, and whether it interrupted it."""

    requested_at: Seconds
    landed: bool = False


class SpanLink(Value):
    """A labelled span's relation ``rel`` (one its kind declares) to the span ``to``."""

    rel: str
    to: SpanId


class SpanLabel(Value):
    """What the search says a span is: one of its declared kinds, with that kind's fields.

    ``attrs_json`` holds the kind's attribute model as JSON (the search's own model, which a
    state schema cannot name); ``verdict`` is one of the kind's verdicts, apart from ``status``.
    """

    kind: str
    title: str | None = None
    links: tuple[SpanLink, ...] = ()
    attrs_json: str | None = None
    verdict: str | None = None
    reason: str | None = None


class Span(Value):
    """One flow task or awaited operation of the run, on the run clock."""

    id: SpanId
    parent: SpanId | None
    type: SpanType
    name: str
    """The flow's name, or the operation's registered name (its kind, or the id root core gave)."""
    status: SpanStatus
    waiting_on: tuple[WaitingOn, ...] = ()
    attempts: tuple[SpanAttempt, ...] = ()
    started_at: Seconds
    ended_at: Seconds | None = None
    failure: SpanFailure | None = None
    stop: SpanStop | None = None
    invocation: str | None = None
    """For an agent turn, the invocation it ran as: what the agent's own records (its usage,
    its logs) name, so the runtime maps them to this span."""
    label: SpanLabel | None = None
    """The search's label of the span (``ctx.label``, or ``label=`` on an aspect call)."""

    @property
    def ended(self) -> bool:
        """Whether the span has ended."""
        return self.ended_at is not None


class RunTrace(Value):
    """Every span of the run, each after its parent, in the order they started."""

    spans: tuple[Span, ...] = ()

    def span(self, span_id: SpanId) -> Span | None:
        """The span ``span_id``, or None."""
        return next((s for s in self.spans if s.id == span_id), None)


class SpanChange(StrEnum):
    """How a span changed between two traces: what a reader of span events is told. Closed."""

    STARTED = "started"
    UPDATED = "updated"
    ENDED = "ended"


def span_changes(before: RunTrace | None, after: RunTrace) -> tuple[tuple[SpanChange, Span], ...]:
    """The changes from ``before`` to ``after``, each with the whole span as it now is.

    A new span is ``started`` (and ``ended`` too when it ended within the same step); a span
    that ends is ``ended``; any other change is ``updated``. Folding the changes in order by span
    id onto ``before`` gives ``after``.
    """
    known = {} if before is None else {span.id: span for span in before.spans}
    found: list[tuple[SpanChange, Span]] = []
    for span in after.spans:
        was = known.get(span.id)
        if was is None:
            found.append((SpanChange.STARTED, span))
            if span.ended:
                found.append((SpanChange.ENDED, span))
        elif was != span:
            ended = span.ended and not was.ended
            found.append((SpanChange.ENDED if ended else SpanChange.UPDATED, span))
    return tuple(found)


def _from_data(value: object) -> object:
    """A span decoded from JSON data: arrays stand for its tuples, strings for its enums."""
    return Span.model_validate(value, strict=False) if isinstance(value, dict) else value


type WireSpan = Annotated[Span, BeforeValidator(_from_data)]
"""A ``Span`` inside a message that crosses a process (a run event): read from JSON data."""
