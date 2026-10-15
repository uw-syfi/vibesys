"""The run trace's read model: one ``Span`` per flow task and per awaited operation.

The runtime records spans where it journals the transitions (see ``_trace``); a reader gets
them frozen, as a ``RunTrace``. Times are on the run clock as the journal recorded them, so a
replay yields the same spans, ids and times.
"""

from __future__ import annotations

from enum import StrEnum

from vs_core.types.common import Identity, Seconds, Value


class SpanId(Identity):
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
    label: None = None
    """The search's label of the span: empty until searches can label spans."""

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
