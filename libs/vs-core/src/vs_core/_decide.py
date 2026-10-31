"""The recovery rule: core state is the fold of the log, and what to do next is a function of it.

There is no blocked or unknown action. An open step with no result runs again; a step is a
request the loop hands to a runner whole, so nothing here names a step kind. The design is
``docs/contributing/run-recovery.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protobuf import Oneof

from ._actions import Action, Cancel, End, Run, Settle, Wait
from ._proofs import request_dependency_status, stop_mode
from ._request_ledger import dispatch_until, retry_limit
from ._request_recovery import is_open
from ._run_park import dispatch_allowed
from ._scope_order import holds_back
from ._step import advance_run_time, step
from ._step_attempts import (
    dispatched,
    epoch_opened,
    finish,
    forgotten,
    interrupted,
    open_record,
    started,
)
from ._step_log import decode_event, decode_interruption, decode_request, decode_result
from .types.common import DependencyStatus, InterruptPolicy, RequestId, RunStatus, StopMode
from .types.records import request_interrupt_policy
from .types.steps import Cancelled, CancelReason, Failed
from .types.until import FailureReason, Until

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_proto.api import Record, StepFinished, StepStarted

    from ._registry import OperationRegistry
    from .types.kernel import CoreState
    from .types.records import RequestRecord


def fold(
    records: Iterable[Record],
    seed: CoreState | None = None,
    *,
    registry: OperationRegistry | None = None,
) -> CoreState:
    """The core state a log leaves, starting from ``seed``.

    Every record names the epoch it was written in and, when it has one, the run clock, which
    excludes downtime. Records that are not core's (spans, metrics, notices) change nothing
    else. A record the reducers refuse is an error, never skipped: the log was written by
    other code. ``registry`` decodes the outcomes of registered operations.
    """
    state = seed
    for record in records:
        state = _apply(state, record, registry)
    if state is None:
        message = "a log folds from a seed state; it holds no record of the run's start"
        raise ValueError(message)
    return state


def decide(state: CoreState, now: float) -> tuple[Action, ...]:
    """What the runner loop does next, for the run clock ``now``. Total: it never raises."""
    state = advance_run_time(state, now)
    open_steps = tuple(record for record in state.records.records if is_open(record))
    if state.run.result is not None and not open_steps:
        return (End(result=state.run.result),)
    actions = tuple(
        action for record in open_steps if (action := _of_step(state, record, now)) is not None
    )
    return actions or (Wait(until=_wake_at(state, open_steps, now)),)


def _apply(
    state: CoreState | None, record: Record, registry: OperationRegistry | None
) -> CoreState | None:
    body = record.body
    if state is None:
        if body is not None and body.field in _CORE_RECORDS:
            message = f"record {record.seq} is core's, and the log holds no seed before it"
            raise ValueError(message)
        return None
    state = _observed(state, record)
    match body:
        case Oneof(field="core_input", value=message):
            return step(state, decode_event(message, registry)).state
        case Oneof(field="step_started", value=message):
            return _start(state, message, registry)
        case Oneof(field="step_finished", value=message):
            return _finish(state, message, registry)
        case Oneof(field="attempt_interrupted", value=message):
            tag = RequestId(root=message.tag)
            return interrupted(state, tag, message.attempt, decode_interruption(message))
        case _:
            return state


def _start(state: CoreState, message: StepStarted, registry: OperationRegistry | None) -> CoreState:
    tag = RequestId(root=message.tag)
    deadline = Until(at=message.deadline_at, watches=open_record(state, tag).until.watches)
    return started(state, tag, message.attempt, decode_request(message, registry), deadline)


def _finish(
    state: CoreState, message: StepFinished, registry: OperationRegistry | None
) -> CoreState:
    tag = RequestId(root=message.tag)
    state = dispatched(state, tag)
    report, owner_events = finish(state, tag, decode_result(message, registry))
    state = step(state, report).state
    for event in owner_events:
        state = step(state, event).state
    if any(is_open(row) for row in state.records.find(tag)):
        problem = f"the log finishes step {tag.root} with an observation that does not end it"
        raise ValueError(problem)
    return forgotten(state, tag)


_CORE_RECORDS = frozenset({"core_input", "step_started", "step_finished", "attempt_interrupted"})


def _observed(state: CoreState, record: Record) -> CoreState:
    """``state`` in the record's epoch and at its run clock."""
    state = epoch_opened(state, record.epoch)
    return state if record.clock is None else advance_run_time(state, record.clock)


def _of_step(state: CoreState, record: RequestRecord, now: float) -> Action | None:
    tag = record.request_id
    attempts = state.steps.of(tag)
    running = attempts is not None and not attempts.ended
    if _run_is_ending(state):
        return (
            Cancel(tag=tag, reason=CancelReason.RUN_ENDING)
            if running
            else Settle(tag=tag, result=Cancelled(reason=CancelReason.RUN_ENDING))
        )
    if running:
        return _stop_running(state, record, now)
    return _start_or_settle(state, record, now)


def _run_is_ending(state: CoreState) -> bool:
    """Whether the run committed a finish that cancels what is open, or has ended."""
    return state.run.result is not None and (
        state.run.status is RunStatus.TERMINAL or stop_mode(state.run) is StopMode.CANCEL
    )


def _stop_running(state: CoreState, record: RequestRecord, now: float) -> Cancel | None:
    tag = record.request_id
    park = state.run.park
    if now >= record.until.at:
        return Cancel(tag=tag, reason=CancelReason.DEADLINE)
    if park is not None and park.deadline_at is not None and now >= park.deadline_at:
        return Cancel(tag=tag, reason=CancelReason.DRAIN_EXPIRED)
    return None


def _start_or_settle(state: CoreState, record: RequestRecord, now: float) -> Run | Settle | None:
    """The step has no attempt running: start the next, or settle it without one."""
    tag = record.request_id
    attempts = state.steps.of(tag)
    dependencies = request_dependency_status(record.request, state.run.receipts, state.records)
    if dependencies is DependencyStatus.FAILED:
        return Settle(tag=tag, result=Cancelled(reason=CancelReason.WITHDRAWN))
    if attempts is not None:
        if request_interrupt_policy(record.request) is InterruptPolicy.CANCEL:
            return Settle(tag=tag, result=Cancelled(reason=CancelReason.INTERRUPTED))
        if attempts.charged > retry_limit(state.run, record.request):
            return Settle(tag=tag, result=Failed(reason=FailureReason.ATTEMPTS_EXHAUSTED))
    if now >= record.until.at:
        return Settle(tag=tag, result=Cancelled(reason=CancelReason.DEADLINE))
    if (
        dependencies is DependencyStatus.PENDING
        or not _may_start(state, record)
        or holds_back(state, record)
    ):
        return None
    deadline = (
        record.until
        if record.dispatched_at is not None
        else dispatch_until(state.run, state.sessions, state.records, state.evaluation, record)
    )
    return Run(
        step=record.request,
        tag=tag,
        attempt=1 if attempts is None else attempts.started + 1,
        deadline_at=deadline.at,
    )


def _may_start(state: CoreState, record: RequestRecord) -> bool:
    """Whether the run's status lets a step start: a paused run starts none but drain work."""
    match state.run.status:
        case RunStatus.RUNNING | RunStatus.CLOSING:
            return True
        case RunStatus.DRAINING:
            return dispatch_allowed(state, record.request)
        case _:
            return False


def _wake_at(state: CoreState, open_steps: tuple[RequestRecord, ...], now: float) -> float | None:
    """The next time anything is due: a step's limit, or the end of a pause's drain."""
    park = state.run.park
    due = [record.until.at for record in open_steps]
    if park is not None and park.deadline_at is not None:
        due.append(park.deadline_at)
    return min((at for at in due if at > now), default=None)
