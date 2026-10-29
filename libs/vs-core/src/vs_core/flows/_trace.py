"""The run trace's recorder: the runtime calls it where it journals each transition.

Every time is ``at``, the run-clock time of the step being handled, which the journal records
with the step; nothing here reads a clock. ``_Run`` owns one ``Trace`` per run and calls it from
the points that already know each fact: starting and ending a task, issuing and answering a
request, suspending on sources, delivering the stop, ``ctx.retry`` and the run's ``Finish``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.types.until import Failed

from ._errors import TaskStopped
from ._flow import Group
from ._retry import GaveUp
from ._sources import Pending, Signal, StopSource, Task, Timer
from ._spans import (
    AttemptOutcome,
    FailureKind,
    RunTrace,
    Span,
    SpanAttempt,
    SpanFailure,
    SpanId,
    SpanLabel,
    SpanStatus,
    SpanStop,
    SpanType,
    SpanWaitKind,
    WaitingOn,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_core.types.common import DecisionId, Seconds
    from vs_core.types.search import Event
    from vs_core.types.settlement import RunResultProposal

    from ._sources import Source

_FLOW = "flow:"
_RESTARTED = "dropped when the code changed"
_ABANDONED = "abandoned: the flow stopped waiting for it"
_RUN_ENDED = "the run ended"
_DESCRIBED = {Timer: SpanWaitKind.TIME, StopSource: SpanWaitKind.STOP}
"""Sources named by their description; any other (a latch) is a slot another flow fills."""
_WORK_IN_PROGRESS = (Pending, Signal, Task, Group)
"""Sources a flow waits on while its own work runs: it is running, not waiting."""


def operation_span(decision: DecisionId) -> SpanId:
    """The span of the operation issued as ``decision``."""
    return f"op:{decision.root}"


class Trace:
    """The spans of one run, recorded as the run moves; ``snapshot`` is the frozen read model."""

    def __init__(self, seed: RunTrace) -> None:
        """Continue the trace ``seed`` (empty for a new run, the last one after a restart)."""
        self.at: Seconds = 0.0
        """The run-clock time of the step being handled."""
        self._spans = {span.id: span for span in seed.spans}
        self._occurrences: dict[str, int] = {}
        for span in seed.spans:
            if span.type is SpanType.FLOW:
                path = _path_of(span.id)
                self._occurrences[path] = self._occurrences.get(path, 0) + 1
        self._pending = {_path_of(s.id): s.id for s in seed.spans if s.status is SpanStatus.PENDING}
        self._stop_at = next((s.stop.requested_at for s in seed.spans if s.stop), None)

    def snapshot(self) -> RunTrace:
        """Every span so far, each after its parent."""
        return RunTrace(spans=tuple(self._spans.values()))

    # -- flows -------------------------------------------------------------------------------

    def flow_started(self, path: str, parent: SpanId | None, name: str, *, stopped: bool) -> SpanId:
        """A task starts at ``path`` (a restored flow keeps the span it had before the restart)."""
        resumed = self._pending.pop(path, None)
        if resumed is not None:
            self._update(resumed, status=SpanStatus.RUNNING)
            return resumed
        number = self._occurrences.get(path, 0) + 1
        self._occurrences[path] = number
        span_id = f"{_FLOW}{path}#{number}"
        stop = SpanStop(requested_at=self._stop_time()) if stopped else None
        self._spans[span_id] = Span(
            id=span_id,
            parent=parent,
            type=SpanType.FLOW,
            name=name,
            status=SpanStatus.RUNNING,
            started_at=self.at,
            stop=stop,
        )
        return span_id

    def suspended(self, span_id: SpanId, sources: Iterable[Source[object]]) -> None:
        """The flow waits on ``sources``; an entry it already waited on keeps its ``since``."""
        span = self._spans[span_id]
        if span.ended:
            return  # the run ended under it: only a refused ``Finish`` wakes it again
        before = {(w.kind, w.ref): w.since for w in span.waiting_on}
        entries, busy = [], False
        for source in sources:
            kind, ref = _waited(source)
            busy = busy or isinstance(source, _WORK_IN_PROGRESS)
            entries.append(WaitingOn(kind=kind, ref=ref, since=before.get((kind, ref), self.at)))
        status = SpanStatus.RUNNING if busy else SpanStatus.WAITING
        self._update(span_id, status=status, waiting_on=tuple(entries))

    def flow_ended(self, span_id: SpanId, result: object) -> None:
        """The task returned ``result`` (``TaskStopped`` when a stop ended it)."""
        match result:
            case TaskStopped():
                self._end(span_id, SpanStatus.STOPPED, _interrupted("stopped"))
            case Failed(reason=reason):
                self._end(span_id, SpanStatus.FAILED, _outcome(reason.value))
            case GaveUp(reasons=reasons):
                self._end(
                    span_id, SpanStatus.FAILED, _outcome(f"gave up after {len(reasons)} tries")
                )
            case _:
                self._end(span_id, SpanStatus.DONE, None)

    def restarted(self, restorable: Iterable[str]) -> None:
        """A code change restarts the flows: what resumes waits pending, the rest is dropped.

        The root and the registered flows at ``restorable`` paths resume from their snapshots
        when started again. Requests in flight and tasks that are not registered flows end.
        """
        resumes = {"", *restorable}
        for span in tuple(self._spans.values()):
            if span.ended:
                continue
            if span.type is SpanType.FLOW and _path_of(span.id) in resumes:
                self._update(span.id, status=SpanStatus.PENDING, waiting_on=())
                self._pending[_path_of(span.id)] = span.id
            else:
                self._end(span.id, SpanStatus.STOPPED, _interrupted(_RESTARTED))

    # -- labels ------------------------------------------------------------------------------

    def exists(self, span_id: SpanId) -> bool:
        """Whether the run has recorded the span ``span_id``."""
        return span_id in self._spans

    def parent_of(self, span_id: SpanId) -> SpanId | None:
        """The parent of ``span_id``; None for the root or a span the run never recorded."""
        found = self._spans.get(span_id)
        return None if found is None else found.parent

    def label_of(self, span_id: SpanId) -> SpanLabel | None:
        """The span's label so far."""
        return self._spans[span_id].label

    def labeled(self, span_id: SpanId, label: SpanLabel) -> None:
        """The search labelled the span (``ctx.label``, or ``label=`` on an aspect call)."""
        self._update(span_id, label=label)

    # -- retries -----------------------------------------------------------------------------

    def attempt_started(self, span_id: SpanId, number: int, of: int) -> None:
        """Try ``number`` of ``of`` of a ``ctx.retry`` starts in the flow."""
        attempt = SpanAttempt(n=number, of=of, started_at=self.at)
        self._update(span_id, attempts=(*self._spans[span_id].attempts, attempt))

    def attempt_ended(self, span_id: SpanId, outcome: AttemptOutcome, reason: str | None) -> None:
        """The flow's current try ended with ``outcome``."""
        *done, last = self._spans[span_id].attempts
        ended = last.model_copy(update={"ended_at": self.at, "outcome": outcome, "reason": reason})
        self._update(span_id, attempts=(*done, ended))

    # -- operations --------------------------------------------------------------------------

    def operation_started(
        self, parent: SpanId, decision: DecisionId, name: str, invocation: str | None = None
    ) -> None:
        """The flow ``parent`` issued ``decision``; it runs until answered or abandoned.

        ``invocation`` is the agent invocation a turn runs as.
        """
        span_id = operation_span(decision)
        self._spans[span_id] = Span(
            id=span_id,
            parent=parent,
            type=SpanType.OPERATION,
            name=name,
            status=SpanStatus.RUNNING,
            started_at=self.at,
            invocation=invocation,
        )

    def operation_answered(self, decision: DecisionId, event: Event) -> None:
        """Core answered ``decision`` with ``event``."""
        span_id = operation_span(decision)
        if isinstance(event, Failed):
            self._end(span_id, SpanStatus.FAILED, _outcome(event.reason.value))
        else:
            self._end(span_id, SpanStatus.DONE, None)

    def operation_abandoned(self, decision: DecisionId) -> None:
        """The flow stopped waiting for ``decision``; a late answer is dropped."""
        span_id = operation_span(decision)
        if not self._spans[span_id].ended:
            self._end(span_id, SpanStatus.STOPPED, _interrupted(_ABANDONED))

    # -- the stop and the end ----------------------------------------------------------------

    def stop_requested(self) -> None:
        """The operator's stop arrived: every live flow is told."""
        self._stop_at = self.at
        for span in tuple(self._spans.values()):
            if span.type is SpanType.FLOW and not span.ended:
                self._update(span.id, stop=SpanStop(requested_at=self.at))

    def stop_lifted(self) -> None:
        """The stop was lifted: a flow it had not interrupted is no longer told of it.

        A flow the stop already interrupted keeps its record of it.
        """
        self._stop_at = None
        for span in tuple(self._spans.values()):
            if span.type is SpanType.FLOW and span.stop is not None and not span.stop.landed:
                self._update(span.id, stop=None)

    def stop_landed(self, span_id: SpanId) -> None:
        """The stop interrupted the flow (``Stopped`` was raised at its await)."""
        self._update(span_id, stop=SpanStop(requested_at=self._stop_time(), landed=True))

    def run_ended(self, root: SpanId, result: RunResultProposal) -> None:
        """The root proposed the run's result: every span still open ends with the run."""
        for span in tuple(self._spans.values()):
            if not span.ended and span.id != root:
                self._end(span.id, SpanStatus.STOPPED, _interrupted(_RUN_ENDED))
        match result.outcome:
            case "success":
                self._end(root, SpanStatus.DONE, None)
            case "cancelled":
                self._end(root, SpanStatus.STOPPED, _interrupted(result.reason))
            case _:
                self._end(root, SpanStatus.FAILED, _outcome(result.reason))

    # -- helpers -----------------------------------------------------------------------------

    def _stop_time(self) -> Seconds:
        return self.at if self._stop_at is None else self._stop_at

    def _update(self, span_id: SpanId, **fields: object) -> None:
        self._spans[span_id] = self._spans[span_id].model_copy(update=fields)

    def _end(self, span_id: SpanId, status: SpanStatus, failure: SpanFailure | None) -> None:
        span = self._spans[span_id]
        attempts = span.attempts
        if attempts and attempts[-1].ended_at is None:
            attempts = (*attempts[:-1], attempts[-1].model_copy(update={"ended_at": self.at}))
        self._update(
            span_id,
            status=status,
            failure=failure,
            ended_at=self.at,
            waiting_on=(),
            attempts=attempts,
        )


def _path_of(span_id: SpanId) -> str:
    return span_id.removeprefix(_FLOW).rpartition("#")[0]


def _outcome(message: str) -> SpanFailure:
    return SpanFailure(kind=FailureKind.OUTCOME, message=message)


def _interrupted(message: str) -> SpanFailure:
    return SpanFailure(kind=FailureKind.INTERRUPTION, message=message)


def _waited(source: Source[object]) -> tuple[SpanWaitKind, str]:
    """What waiting on ``source`` is, as a kind and a reference."""
    match source:
        case Pending():
            return SpanWaitKind.OPERATION, operation_span(source.decision)
        case Signal():
            return SpanWaitKind.OPERATION, source.label
        case Task(span=str() as child):
            return SpanWaitKind.SLOT, child
        case Group():
            return SpanWaitKind.SLOT, "group"
        case _:
            return _DESCRIBED.get(type(source), SpanWaitKind.SLOT), source.describe()
