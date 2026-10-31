"""The recovery rule: core state is the fold of the log, and what to do next is a function of it.

There is no blocked or unknown action. An open step with no result runs again; a step is a
request the loop hands to a runner whole, so nothing here names a step kind. The design is
``docs/contributing/run-recovery.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protobuf import Oneof

from ._actions import Action, Cancel, End, Run, RunRest, Settle, Wait, at_rest
from ._proofs import request_dependency_status, stop_mode
from ._refusal import RecordRefusal, RecordRefusedError, RefusalReason
from ._request_ledger import dispatch_until, retry_limit
from ._request_recovery import is_open
from ._run_park import pause_cancels, starts
from ._step import advance_run_time, settle_end, settle_park, step, step_recorded
from ._step_attempts import (
    commands_lapsed,
    concluded,
    dispatched,
    end_lifted,
    ended,
    ended_by_core,
    ended_unconcluded,
    epoch_opened,
    finish,
    forgotten,
    interrupted,
    open_record,
    reported_by,
    started,
)
from ._step_log import (
    decode_event,
    decode_interruption,
    decode_request,
    decode_result,
    decode_run_ended,
)
from .types.common import (
    DependencyStatus,
    InterruptPolicy,
    LifecycleClass,
    RequestId,
    RunStatus,
    StopMode,
)
from .types.records import request_interrupt_policy, request_lifecycle
from .types.steps import Cancelled, CancelReason, Failed
from .types.until import FailureReason, Until

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_proto.api import Record, StepFinished, StepStarted

    from ._registry import OperationRegistry
    from .types.kernel import CoreState
    from .types.records import RequestRecord
    from .types.steps import StepResult


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
        raise RecordRefusedError(RefusalReason.NO_SEED, message)
    return state


def decide(state: CoreState, now: float) -> tuple[Action, ...]:
    """What the runner loop does next, for the run clock ``now``. Total: it never raises.

    An ended run is over. A parked run starts and settles nothing (a step it holds waits for the
    epoch that resumes the run), but an attempt it still has running is cancelled: the kernel
    parks at a Pause now before the loop has stopped what runs, and a flight nothing cancels
    would hold the process up.
    """
    state = advance_run_time(state, now)
    rest = at_rest(state)
    if rest is RunRest.ENDED:
        return (Wait(until=None),)
    open_steps = tuple(record for record in state.records.records if is_open(record))
    if rest is RunRest.PARKED:
        stops = (
            *(
                Cancel(tag=record.request_id, reason=CancelReason.PAUSE_NOW)
                for record in open_steps
                if _running(state, record)
            ),
            *(action for action in _stopped_by_core(state, now) if isinstance(action, Cancel)),
        )
        return stops or (Wait(until=None),)
    if state.run.result is not None and not open_steps and state.run.status is RunStatus.TERMINAL:
        return (End(result=state.run.result),)
    actions = (
        *_stopped_by_core(state, now),
        *(action for record in open_steps if (action := _of_step(state, record, now)) is not None),
    )
    return actions or (Wait(until=_wake_at(state, open_steps, now)),)


def _running(state: CoreState, record: RequestRecord) -> bool:
    attempts = state.steps.of(record.request_id)
    return attempts is not None and not attempts.ended


def start_due(state: CoreState, tag: RequestId, now: float) -> Run | RecordRefusal:
    """The attempt the rule would ``Run`` for the step ``tag`` at the run clock ``now``, or why not.

    The fold refuses a ``StepStarted`` that is not this, so the log never holds a start the rule
    forbids. A start is checked and stamped at one ``now``: a ``Run`` decided earlier carries the
    limit of its own instant (an unstarted step gets ``now`` plus its limit), and a pause, a park
    or the end of the step's time may have come since. A flight that waited (for a ``clear``,
    or for a writer) asks again at the instant the log stamps its start, and records what this
    returns. The refusal is ``STEP_NOT_OPEN`` or ``START_NOT_DUE``, the two a race explains.
    """
    record = state.records.record(tag)
    if record is None or not is_open(record):
        return RecordRefusal(
            RefusalReason.STEP_NOT_OPEN, tag, f"the log names step {tag.root}, which is not open"
        )
    action = _of_step(advance_run_time(state, now), record, now)
    if isinstance(action, Run):
        return action
    return RecordRefusal(
        RefusalReason.START_NOT_DUE,
        tag,
        f"the log starts step {tag.root} in a run that may not start it",
    )


def may_start(state: CoreState, tag: RequestId, now: float) -> bool:
    """Whether the run lets the step ``tag`` start an attempt at the run clock ``now``."""
    return isinstance(start_due(state, tag, now), Run)


def _apply(
    state: CoreState | None, record: Record, registry: OperationRegistry | None
) -> CoreState | None:
    body = record.body
    if state is None:
        if body is not None and body.field in _CORE_RECORDS:
            message = f"record {record.seq} is core's, and the log holds no seed before it"
            raise RecordRefusedError(RefusalReason.NO_SEED, message)
        return None
    state = _observed(state, record)
    match body:
        case Oneof(field="core_input", value=message):
            state = commands_lapsed(end_lifted(step(state, decode_event(message, registry)).state))
        case Oneof(field="step_started", value=message):
            state = _start(state, message, registry)
        case Oneof(field="step_finished", value=message):
            state = _finish(state, message, registry)
        case Oneof(field="attempt_interrupted", value=message):
            why = decode_interruption(message)
            state = settle_park(
                interrupted(state, RequestId(root=message.tag), message.attempt, why)
            )
        case Oneof(field="run_ended", value=message):
            state = ended(state, decode_run_ended(message))
    return settle_end(state)


def _start(state: CoreState, message: StepStarted, registry: OperationRegistry | None) -> CoreState:
    tag = RequestId(root=message.tag)
    record = open_record(state, tag)
    due = start_due(state, tag, state.run.now_at)
    if isinstance(due, RecordRefusal):
        raise RecordRefusedError(due.reason, due.detail, due.tag)
    deadline = Until(at=message.deadline_at, watches=record.until.watches)
    return started(state, tag, message.attempt, decode_request(message, registry), deadline)


def _finish(
    state: CoreState, message: StepFinished, registry: OperationRegistry | None
) -> CoreState:
    tag = RequestId(root=message.tag)
    if ended_by_core(state, tag):
        return settle_park(_concluded_by_core(state, tag, decode_result(message, registry)))
    reported_by(state, tag, message.attempt)
    state = dispatched(state, tag)
    report, owner_events = finish(state, tag, decode_result(message, registry))
    state = step_recorded(state, report).state
    for event in owner_events:
        state = step_recorded(state, event).state
    if any(is_open(row) for row in state.records.find(tag)):
        problem = f"the log finishes step {tag.root} with an observation that does not end it"
        raise RecordRefusedError(RefusalReason.RESULT_MISMATCH, problem, tag)
    return settle_park(forgotten(state, tag))


def _concluded_by_core(state: CoreState, tag: RequestId, result: StepResult) -> CoreState:
    """``state`` once the end of an attempt of a step core ended is recorded.

    What the runner reports is not applied: core ended the step. A step core blocked is concluded
    by the cancel it asked for, whose reason it keeps.
    """
    if any(record.request_id == tag for record in ended_unconcluded(state)):
        reason = result.reason if isinstance(result, Cancelled) else CancelReason.DEADLINE
        state = step_recorded(state, concluded(state, tag, reason)).state
    return forgotten(state, tag)


_CORE_RECORDS = frozenset(
    {"core_input", "step_started", "step_finished", "attempt_interrupted", "run_ended"}
)


def _observed(state: CoreState, record: Record) -> CoreState:
    """``state`` in the record's epoch and at its run clock."""
    opened = epoch_opened(state, record.epoch)
    if opened is not state:
        opened = settle_park(opened)
    return opened if record.clock is None else advance_run_time(opened, record.clock)


def _stopped_by_core(state: CoreState, now: float) -> tuple[Cancel | Settle, ...]:
    """The steps core ended itself (at their limit, say) that the loop has to end for good.

    A flight still runs for such a step: the cancel ends the attempt. The runner takes the reason
    as it takes any other (a limit that passed, or the asker no longer wanting the result). A step
    core ended that still holds the run's closure (a job it owns is not yet released) and has no
    flight is settled, and either record concludes the step, so the run can close.
    """
    stopped: list[Cancel | Settle] = []
    for record in ended_unconcluded(state):
        reason = CancelReason.DEADLINE if now >= record.until.at else CancelReason.WITHDRAWN
        if _running(state, record):
            stopped.append(Cancel(tag=record.request_id, reason=reason))
        else:
            stopped.append(Settle(tag=record.request_id, result=Cancelled(reason=reason)))
    stopped.extend(
        Cancel(tag=row.tag, reason=CancelReason.WITHDRAWN)
        for row in state.steps.attempts
        if not row.ended
        and ended_by_core(state, row.tag)
        and all(row.tag != record.request_id for record in ended_unconcluded(state))
    )
    return tuple(stopped)


def _of_step(state: CoreState, record: RequestRecord, now: float) -> Action | None:
    tag = record.request_id
    running = _running(state, record)
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
    if park is not None and pause_cancels(state, record.request):
        released = request_lifecycle(record.request) is LifecycleClass.OWNED_JOB
        reason = (
            CancelReason.PAUSE_NOW if park.immediate or released else CancelReason.DRAIN_EXPIRED
        )
        return Cancel(tag=tag, reason=reason)
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
    if not starts(state, record, dependencies):
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


def _wake_at(state: CoreState, open_steps: tuple[RequestRecord, ...], now: float) -> float | None:
    """The next time anything is due: a step's limit, or the end of a pause's drain."""
    park = state.run.park
    due = [record.until.at for record in open_steps]
    if park is not None and park.deadline_at is not None:
        due.append(park.deadline_at)
    return min((at for at in due if at > now), default=None)
