"""The attempts of open steps, as the log records them: what the fold writes and ``decide`` reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._request_recovery import is_open
from .types.common import EventId, Observation, ObservationStatus
from .types.records import RequestObserved, RequestPhase, RequestRecord
from .types.steps import (
    Cancelled,
    Failed,
    Finished,
    Interruption,
    StepAttempts,
    StepsState,
)

if TYPE_CHECKING:
    from .types.common import RequestId
    from .types.kernel import CoreState
    from .types.steps import StepResult
    from .types.until import Until


def open_record(state: CoreState, tag: RequestId) -> RequestRecord:
    """The open step ``tag``; a log that names another is not one this code wrote."""
    rows = state.records.find(tag)
    if len(rows) != 1 or not is_open(rows[0]):
        message = f"the log names step {tag.root}, which is not open"
        raise ValueError(message)
    return rows[0]


def epoch_opened(state: CoreState, epoch: int) -> CoreState:
    """``state`` in epoch ``epoch``, with every attempt of an earlier epoch ended.

    A run has one writer, so a record of a later epoch proves the process that ran the earlier
    epoch's attempts dead. They end without a result and count against the retry bound: the
    attempt may have run, and a step that kills its process every time must end as a typed failure.
    """
    if epoch <= state.steps.epoch:
        return state
    rows = tuple(row.model_copy(update={"ended": True}) for row in state.steps.attempts)
    return state.model_copy(update={"steps": StepsState(epoch=epoch, attempts=rows)})


def started(
    state: CoreState, tag: RequestId, attempt: int, request: object, deadline: Until
) -> CoreState:
    """``state`` with attempt ``attempt`` of ``tag`` begun in the epoch the log is in."""
    record = open_record(state, tag)
    previous = state.steps.of(tag)
    expected = 1 if previous is None else previous.started + 1
    if attempt != expected or record.request != request or (previous and not previous.ended):
        message = f"the log starts attempt {attempt} of step {tag.root}, not its attempt {expected}"
        raise ValueError(message)
    if record.phase is RequestPhase.PREPARED:
        record = record.model_copy(
            update={
                "phase": RequestPhase.DISPATCHED,
                "until": deadline,
                "dispatched_at": state.run.now_at,
            }
        )
    row = StepAttempts(
        tag=tag, started=attempt, uncharged=0 if previous is None else previous.uncharged
    )
    return _with(_replaced(state, record), tag, row)


def interrupted(state: CoreState, tag: RequestId, attempt: int, why: Interruption) -> CoreState:
    """``state`` with the running attempt of ``tag`` ended by ``why``, charged only if it raised."""
    open_record(state, tag)
    row = state.steps.of(tag)
    if row is None or row.started != attempt or row.ended:
        message = f"the log interrupts attempt {attempt} of step {tag.root}, which is not running"
        raise ValueError(message)
    uncharged = row.uncharged + (0 if why.charged else 1)
    return _with(state, tag, row.model_copy(update={"ended": True, "uncharged": uncharged}))


def dispatched(state: CoreState, tag: RequestId) -> CoreState:
    """``state`` with the open step ``tag`` out of PREPARED.

    The ledger takes an observation only of a dispatched request. A step core settles without
    ever running it (a limit passed in the queue, its dependency failed) is dispatched at that
    instant, so the settlement is an ordinary conclusive observation.
    """
    record = open_record(state, tag)
    if record.phase is not RequestPhase.PREPARED:
        return state
    return _replaced(state, record.model_copy(update={"phase": RequestPhase.DISPATCHED}))


def report_of(state: CoreState, tag: RequestId, result: StepResult) -> RequestObserved:
    """The report the ledger takes for the result of ``tag``.

    A finished step brings the executor's own; for a failure or cancellation core decided, core
    writes the conclusive observation itself.
    """
    record = open_record(state, tag)
    match result:
        case Finished():
            if result.observation.request_id != tag:
                message = f"the log finishes step {tag.root} with another step's observation"
                raise ValueError(message)
            return result.report
        case Failed() | Cancelled():
            return _concluded(record, result, state.run.now_at)


def forgotten(state: CoreState, tag: RequestId) -> CoreState:
    """``state`` without the attempts of ``tag``: its step has a result."""
    rows = tuple(other for other in state.steps.attempts if other.tag != tag)
    return state.model_copy(update={"steps": state.steps.model_copy(update={"attempts": rows})})


def _concluded(record: RequestRecord, result: Failed | Cancelled, now_at: float) -> RequestObserved:
    """The conclusive observation the ledger takes for a step core ended without a report."""
    prior = record.observation
    cancelled = isinstance(result, Cancelled)
    return RequestObserved(
        observation=Observation(
            event_id=EventId(root=f"step-finished:{record.request_id.root}"),
            request_id=record.request_id,
            scope=record.request.scope,
            admission_id=record.request.admission_id,
            sequence=0 if prior is None else prior.sequence + 1,
            observed_at=now_at,
            status=ObservationStatus.CANCELLED if cancelled else ObservationStatus.FAILED,
            terminal=True,
            released=True,
            children_complete=True,
            diagnostic=result.reason.value,
        )
    )


def _replaced(state: CoreState, record: RequestRecord) -> CoreState:
    rows = list(state.records.records)
    for position in state.records.positions(record.request_id):
        rows[position] = record
    return state.model_copy(
        update={"records": state.records.model_copy(update={"records": tuple(rows)})}
    )


def _with(state: CoreState, tag: RequestId, row: StepAttempts) -> CoreState:
    rows = tuple(other for other in state.steps.attempts if other.tag != tag)
    steps = state.steps.model_copy(update={"attempts": (*rows, row)})
    return state.model_copy(update={"steps": steps})
