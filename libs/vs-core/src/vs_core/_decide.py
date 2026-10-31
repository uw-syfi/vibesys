"""The recovery rule: core state is the fold of the log, and what to do next is a function of it.

There is no blocked or unknown action. An open step with no result runs again; a step is a
request the loop hands to a runner whole, so nothing here names a step kind. The design is
``docs/contributing/run-recovery.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, overload

from protobuf import Oneof

from ._actions import Action, Cancel, End, Run, RunRest, Settle, Wait, at_rest
from ._proofs import request_dependency_status, stop_mode
from ._request_ledger import dispatch_until, retry_limit
from ._request_recovery import is_open
from ._run_park import pause_cancels, starts
from ._search_events import Translated, translate
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
    decode_proposals,
    decode_request,
    decode_result,
    decode_run_ended,
    decode_seed,
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

    from vs_proto.api import Record, StepFinished, StepStarted, StrategyAdvanced

    from ._registry import OperationRegistry
    from .types.kernel import CoreEvent, CoreState, StrategyEvent
    from .types.records import RequestRecord
    from .types.steps import StepResult


@dataclass(frozen=True)
class Folded:
    """A log's fold: core's state, and what the search still has to hear.

    ``owed`` is what core published, as the search receives it, since the last advance record
    (or since the run began): events whose advance a crash may have cut off. The search hears
    them before anything else when a process takes the log over.
    """

    state: CoreState
    owed: Translated = field(default_factory=Translated)


def fold(
    records: Iterable[Record],
    seed: CoreState | None = None,
    *,
    registry: OperationRegistry | None = None,
) -> CoreState:
    """The core state a log leaves, starting from ``seed`` or the log's ``RunStarted``.

    Every record names the epoch it was written in and, when it has one, the run clock, which
    excludes downtime. Records that are not core's (spans, metrics, notices) change nothing
    else. A record the reducers refuse is an error, never skipped: the log was written by
    other code. ``registry`` decodes the outcomes of registered operations.
    """
    return fold_run(records, seed, registry=registry).state


def fold_run(
    records: Iterable[Record],
    seed: CoreState | None = None,
    *,
    registry: OperationRegistry | None = None,
) -> Folded:
    """``fold``, and the events the search still has to hear."""
    folded = None if seed is None else Folded(seed)
    for record in records:
        folded = apply_record(folded, record, registry)
    if folded is None:
        message = "a log folds from a seed state; it holds no record of the run's start"
        raise ValueError(message)
    return folded


@overload
def apply_record(
    prior: Folded, record: Record, registry: OperationRegistry | None = None
) -> Folded: ...


@overload
def apply_record(
    prior: Folded | None, record: Record, registry: OperationRegistry | None = None
) -> Folded | None: ...


def apply_record(
    prior: Folded | None, record: Record, registry: OperationRegistry | None = None
) -> Folded | None:
    """``prior`` with one more record folded in; None while the log has not started the run."""
    state = None if prior is None else prior.state
    body = record.body
    if state is None or (body is not None and body.field == "run_started"):
        return _begin(state, record, registry)
    state = _observed(state, record)
    if body is not None and body.field == "strategy_advanced":
        # The search heard everything owed before it answered, and what its proposals caused.
        return Folded(_advanced(state, body.value, registry))
    state, published = _apply(state, body, registry)
    heard = translate(state, published)
    owed = prior.owed if prior is not None else Translated()
    return Folded(
        state, Translated((*owed.events, *heard.events), (*owed.discarded, *heard.discarded))
    )


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


def may_start(state: CoreState, tag: RequestId, now: float) -> bool:
    """Whether the run lets the step ``tag`` start an attempt at the run clock ``now``.

    That is when the rule would ``Run`` it. The fold refuses a start the rule forbids, so the log
    never holds one. A flight that waited (for a ``clear``) while the run paused, parked or ran
    out the step's time asks again, with the clock it records its start at, before it does.
    """
    record = state.records.record(tag)
    if record is None or not is_open(record):
        return False
    return isinstance(_of_step(advance_run_time(state, now), record, now), Run)


def _begin(
    state: CoreState | None, record: Record, registry: OperationRegistry | None
) -> Folded | None:
    """What a record that is, or precedes, the run's seed leaves of the fold."""
    body = record.body
    if body is not None and body.field == "run_started":
        if state is not None:
            message = f"record {record.seq} starts a run the log already started"
            raise ValueError(message)
        return Folded(_observed(decode_seed(body.value, registry), record))
    if body is not None and body.field in _CORE_RECORDS:
        message = f"record {record.seq} is core's, and the log holds no seed before it"
        raise ValueError(message)
    return None


def _apply(
    state: CoreState, body: Oneof | None, registry: OperationRegistry | None
) -> tuple[CoreState, tuple[StrategyEvent, ...]]:
    """``state`` after one record, and the events core published in folding it."""
    published: tuple[StrategyEvent, ...] = ()
    match body:
        case Oneof(field="core_input", value=message):
            state, published = _stepped(state, decode_event(message, registry))
        case Oneof(field="step_started", value=message):
            state = _start(state, message, registry)
        case Oneof(field="step_finished", value=message):
            state, published = _finish(state, message, registry)
        case Oneof(field="attempt_interrupted", value=message):
            why = decode_interruption(message)
            state = settle_park(
                interrupted(state, RequestId(root=message.tag), message.attempt, why)
            )
        case Oneof(field="run_ended", value=message):
            state = ended(state, decode_run_ended(message))
    return settle_end(state), published


def _stepped(state: CoreState, event: CoreEvent) -> tuple[CoreState, tuple[StrategyEvent, ...]]:
    """``state`` after one input of core's, and the events it published.

    A log has no timer and no executor, so what the kernel leaves for them is settled here: the
    commands it registered lapse, and the end a resume reopened is dropped.
    """
    transition = step(state, event)
    return commands_lapsed(end_lifted(transition.state)), transition.events


def _advanced(
    state: CoreState, message: StrategyAdvanced, registry: OperationRegistry | None
) -> CoreState:
    """``state`` after the proposals core took in an advance."""
    for proposal in decode_proposals(message, registry):
        state, _ = _stepped(state, proposal)
    return settle_end(state)


def _start(state: CoreState, message: StepStarted, registry: OperationRegistry | None) -> CoreState:
    tag = RequestId(root=message.tag)
    record = open_record(state, tag)
    if not may_start(state, tag, state.run.now_at):
        problem = f"the log starts step {tag.root} in a run that may not start it"
        raise ValueError(problem)
    deadline = Until(at=message.deadline_at, watches=record.until.watches)
    return started(state, tag, message.attempt, decode_request(message, registry), deadline)


def _finish(
    state: CoreState, message: StepFinished, registry: OperationRegistry | None
) -> tuple[CoreState, tuple[StrategyEvent, ...]]:
    tag = RequestId(root=message.tag)
    if ended_by_core(state, tag):
        state, published = _concluded_by_core(state, tag, decode_result(message, registry))
        return settle_park(state), published
    reported_by(state, tag, message.attempt)
    state = dispatched(state, tag)
    report, owner_events = finish(state, tag, decode_result(message, registry))
    transition = step_recorded(state, report)
    state, published = transition.state, transition.events
    for event in owner_events:
        transition = step_recorded(state, event)
        state, published = transition.state, (*published, *transition.events)
    if any(is_open(row) for row in state.records.find(tag)):
        problem = f"the log finishes step {tag.root} with an observation that does not end it"
        raise ValueError(problem)
    return settle_park(forgotten(state, tag)), published


def _concluded_by_core(
    state: CoreState, tag: RequestId, result: StepResult
) -> tuple[CoreState, tuple[StrategyEvent, ...]]:
    """``state`` once the end of an attempt of a step core ended is recorded, and what it published.

    What the runner reports is not applied: core ended the step. A step core blocked is concluded
    by the cancel it asked for, whose reason it keeps.
    """
    published: tuple[StrategyEvent, ...] = ()
    if any(record.request_id == tag for record in ended_unconcluded(state)):
        reason = result.reason if isinstance(result, Cancelled) else CancelReason.DEADLINE
        transition = step_recorded(state, concluded(state, tag, reason))
        state, published = transition.state, transition.events
    return forgotten(state, tag), published


_CORE_RECORDS = frozenset(
    {
        "core_input",
        "strategy_advanced",
        "step_started",
        "step_finished",
        "attempt_interrupted",
        "run_ended",
    }
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
