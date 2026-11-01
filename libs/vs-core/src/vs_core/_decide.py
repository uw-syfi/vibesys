"""The recovery rule: core state is the fold of the log, and what to do next is a function of it.

There is no blocked or unknown action. An open step with no result runs again; a step is a
request the loop hands to a runner whole, so nothing here names a step kind. The design is
``docs/contributing/run-recovery.md``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, overload

from protobuf import Oneof

from ._actions import Action, Cancel, End, Run, RunRest, Settle, Tick, Wait, at_rest
from ._proofs import request_dependency_status, stop_mode
from ._refusal import RecordRefusal, RecordRefusedError, RefusalReason
from ._request_ledger import dispatch_until, retry_limit
from ._request_recovery import is_open
from ._run_park import pause_cancels, starts
from ._scope_order import withdrawn_by_close
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
    decode_fold,
    decode_interruption,
    decode_proposals,
    decode_request,
    decode_result,
    decode_run_ended,
    decode_seed,
    encode_fold,
)
from ._wait_clock import job_step_until
from .types.common import (
    DependencyStatus,
    EndPolicy,
    InterruptPolicy,
    LifecycleClass,
    RequestId,
    RunStatus,
    StopMode,
)
from .types.records import request_end_policy, request_interrupt_policy, request_lifecycle
from .types.steps import Cancelled, CancelReason, Failed
from .types.until import FailureReason, Until

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_proto.api import Record, StepFinished, StepStarted, StrategyAdvanced

    from ._registry import OperationRegistry
    from ._step_log import SavedRun
    from .types.kernel import CoreEvent, CoreState, StrategyEvent
    from .types.records import RequestRecord
    from .types.steps import StepAttempts, StepResult


@dataclass(frozen=True)
class Folded:
    """A log's fold: core's state, and what the search still has to hear.

    ``owed`` is what core published, as the search receives it, since the last advance record
    (or since the run began): events whose advance a crash may have cut off. The search hears
    them before anything else when a process takes the log over.
    """

    state: CoreState
    owed: Translated = field(default_factory=Translated)

    def encode(self) -> bytes:
        """The canonical JSON of this fold, for a cache of it."""
        return encode_fold(self.state, self.owed.events, self.owed.discarded)

    @classmethod
    def decode(cls, data: bytes, registry: OperationRegistry | None = None) -> Folded:
        """The fold ``encode`` wrote, refused with a ``ValueError`` when it cannot be read."""
        state, events, discarded = decode_fold(data, registry)
        return cls(state, Translated(events, discarded))


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
    return fold_run(records, None if seed is None else Folded(seed), registry=registry).state


def fold_run(
    records: Iterable[Record],
    seed: Folded | None = None,
    *,
    registry: OperationRegistry | None = None,
) -> Folded:
    """``fold``, and the events the search still has to hear.

    ``seed`` may be the fold of the log's first records (a cache of it, or a saved run's seed), and
    ``records`` then the ones after: the result is the fold of the whole log.
    """
    folded = seed
    for record in records:
        folded = apply_record(folded, record, registry)
    if folded is None:
        message = "a log folds from a seed state; it holds no record of the run's start"
        raise RecordRefusedError(RefusalReason.NO_SEED, message)
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
    body = record.body
    if prior is None or (body is not None and body.field in _SEEDS):
        return _begin(prior, record, registry)
    state = _observed(prior.state, record)
    if body is not None and body.field == "strategy_advanced":
        # The search heard everything owed before it answered, and what its proposals caused.
        return Folded(_advanced(state, body.value, registry))
    return _heard(prior, *_apply(state, body, registry))


def _heard(prior: Folded, state: CoreState, published: tuple[StrategyEvent, ...]) -> Folded:
    """``state`` as the fold of one more record, which published ``published`` for the search."""
    heard = translate(state, published)
    owed = prior.owed
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
    recorded = state
    state = advance_run_time(state, now)
    rest = at_rest(state)
    if rest is RunRest.ENDED:
        return (Wait(until=None),)
    if _park_is_due(recorded, state):
        return (Tick(at=now),)
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


def _park_is_due(recorded: CoreState, now: CoreState) -> bool:
    """Whether the time that passed parks a draining run, which no record has yet said.

    The drain of a pause stops waiting at its deadline, so a run that waited only on attempts
    the loop cancels parks once the clock reaches it. The log shows that only through a record,
    and ending a step is not always the next one: the run clock is an input (``Tick``), so the
    park is in the log whatever else the run does.
    """
    return (
        recorded.run.status is RunStatus.DRAINING
        and settle_park(now).run.status is RunStatus.PARKED
    )


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
    advanced = advance_run_time(state, now)
    action = None if _park_is_due(state, advanced) else _of_step(advanced, record, now)
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


def _begin(
    prior: Folded | None, record: Record, registry: OperationRegistry | None
) -> Folded | None:
    """What a record that is, or precedes, the run's seed leaves of the fold."""
    body = record.body
    if prior is not None and starts_run(record):
        message = f"record {record.seq} starts a run the log already started"
        raise RecordRefusedError(RefusalReason.SEED_REPEATED, message)
    match body:
        case Oneof(field="run_started", value=seed):
            return Folded(_observed(decode_seed(seed, registry), record))
        case Oneof(field="core_snapshot"):
            message = (
                f"record {record.seq} is a snapshot: its run is decoded by the log's reader "
                "and folded with apply_saved"
            )
            raise RecordRefusedError(RefusalReason.UNREADABLE, message)
        case Oneof(field=field) if field in _CORE_RECORDS:
            message = f"record {record.seq} is core's, and the log holds no seed before it"
            raise RecordRefusedError(RefusalReason.NO_SEED, message)
        case _:
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
    commands it registered lapse, the end a resume reopened is dropped, and a drain that has
    nothing left to wait for parks.
    """
    transition = step(state, event)
    return settle_park(commands_lapsed(end_lifted(transition.state))), transition.events


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
    due = start_due(state, tag, state.run.now_at)
    if isinstance(due, RecordRefusal):
        raise RecordRefusedError(due.reason, due.detail, due.tag)
    deadline = Until(at=message.deadline_at, watches=record.until.watches)
    return started(state, tag, message.attempt, decode_request(message, registry), deadline)


def _finish(
    state: CoreState, message: StepFinished, registry: OperationRegistry | None
) -> tuple[CoreState, tuple[StrategyEvent, ...]]:
    tag = RequestId(root=message.tag)
    by_core = ended_by_core(state, tag)
    reported_by(state, tag, message.attempt)  # core ending the step excuses no stale attempt
    if by_core:
        state, published = _concluded_by_core(state, tag, decode_result(message, registry))
        return settle_park(state), published
    state = dispatched(state, tag)
    report, owner_events = finish(state, tag, decode_result(message, registry))
    transition = step_recorded(state, report)
    state, published = transition.state, transition.events
    for event in owner_events:
        transition = step_recorded(state, event)
        state, published = transition.state, (*published, *transition.events)
    if any(is_open(row) for row in state.records.find(tag)):
        problem = f"the log finishes step {tag.root} with an observation that does not end it"
        raise RecordRefusedError(RefusalReason.RESULT_MISMATCH, problem, tag)
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


_SEEDS = frozenset({"run_started", "core_snapshot"})
"""The records a fold starts from: a new run's seed, and the snapshot of a run an older version saved."""


def starts_run(record: Record) -> bool:
    """Whether ``record`` is the seed a fold starts from: a new run's, or a saved run's snapshot."""
    return record.body is not None and record.body.field in _SEEDS


def apply_saved(prior: Folded | None, saved: SavedRun, record: Record) -> Folded:
    """The fold a saved run's snapshot ``record`` starts, with its unapplied inputs applied in order.

    ``saved`` is the run the record holds, decoded by the log's reader. The record gives the epoch
    and the run clock the fold begins in. It is the log's seed, so ``prior`` is None.
    """
    if prior is not None:
        message = f"record {record.seq} starts a run the log already started"
        raise RecordRefusedError(RefusalReason.SEED_REPEATED, message)
    folded = Folded(_observed(saved.state, record))
    for event in saved.inputs:
        state, published = _stepped(folded.state, event)
        folded = _heard(folded, settle_end(state), published)
    return folded


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
    if _run_ends_step(state, record):
        return (
            Cancel(tag=tag, reason=CancelReason.RUN_ENDING)
            if running
            else Settle(tag=tag, result=Cancelled(reason=CancelReason.RUN_ENDING))
        )
    if withdrawn_by_close(state, record):
        return (
            Cancel(tag=tag, reason=CancelReason.WITHDRAWN)
            if running
            else Settle(tag=tag, result=Cancelled(reason=CancelReason.WITHDRAWN))
        )
    if running:
        return _stop_running(state, record, now)
    return _start_or_settle(state, record, now)


def _run_is_ending(state: CoreState) -> bool:
    """Whether the run committed a finish that cancels what is open, or has ended."""
    return state.run.result is not None and (
        state.run.status is RunStatus.TERMINAL or stop_mode(state.run) is StopMode.CANCEL
    )


def _run_ends_step(state: CoreState, record: RequestRecord) -> bool:
    """Whether the run's ending cancels the open step ``record`` instead of letting it run.

    A run that ended holds nothing it could still free, so it ends every step. A run that is
    ending by a cancel ends the steps its kind declares as work (``EndPolicy.CANCEL``) and lets
    the ones that free what the run holds (``EndPolicy.RELEASE``) run on: the run is closing
    until they have their results, and cancelling them would leave it closing until the host
    gave up on it.
    """
    if not _run_is_ending(state):
        return False
    return (
        state.run.status is RunStatus.TERMINAL
        or request_end_policy(record.request, state.registry) is EndPolicy.CANCEL
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
    if not starts(state, record, dependencies) or now < _retry_at(state, attempts):
        return None
    return Run(
        step=record.request,
        tag=tag,
        attempt=1 if attempts is None else attempts.started + 1,
        deadline_at=_started_until(state, record).at,
        clear_first=attempts is not None,
    )


def _retry_at(state: CoreState, attempts: StepAttempts | None) -> float:
    """The run clock at which the step's next attempt may start: the backoff of its charges has passed.

    The wait counts from the record that last charged an attempt and doubles with each charge
    (``Limits.request_retries``), so a runner that raises at once, or a host that dies each time,
    spends its tries over time and not in one instant. A step with no charge starts at once, and so
    does one of a run that is ending: ending a run needs no time (rule 4), so its cleanup retries
    at once, still within the step's bound.
    """
    if attempts is None or attempts.charged_at is None or _run_is_ending(state):
        return -math.inf
    return attempts.charged_at + state.run.limits.request_retries.delay(attempts.charged)


def _started_until(state: CoreState, record: RequestRecord) -> Until:
    """The limit of the step's first attempt: the one it started with, or the one it would get now.

    A job step also gets the queue budget before its limit counts (``job_step_until``).
    """
    if record.dispatched_at is not None:
        return record.until
    until = dispatch_until(state.run, state.sessions, state.records, state.evaluation, record)
    if request_lifecycle(record.request) is LifecycleClass.OWNED_JOB:
        return job_step_until(state.run, until)
    return until


def _wake_at(state: CoreState, open_steps: tuple[RequestRecord, ...], now: float) -> float | None:
    """The next time anything is due: a step's limit, or the end of a pause's drain."""
    park = state.run.park
    due = [record.until.at for record in open_steps]
    due.extend(
        _retry_at(state, attempts)
        for record in open_steps
        if (attempts := state.steps.of(record.request_id)) is not None and attempts.ended
    )
    if park is not None and park.deadline_at is not None:
        due.append(park.deadline_at)
    return min((at for at in due if at > now), default=None)
