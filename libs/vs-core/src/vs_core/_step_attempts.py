"""The attempts of open steps, as the log records them: what the fold writes and ``decide`` reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._actions import OpenStep
from ._ownership import unreleased_records
from ._refusal import RecordRefusedError, RefusalReason
from ._request_recovery import command_open, is_open, is_step, resent
from .types.common import EventId, Observation, ObservationStatus, RunStatus
from .types.evaluation import JobObserved, SubmitMeasurement
from .types.records import RequestObserved, RequestPhase, RequestRecord
from .types.steps import (
    Cancelled,
    CancelReason,
    Failed,
    Finished,
    Interruption,
    OwnerEvent,
    StepAttempts,
)
from .types.until import Until

if TYPE_CHECKING:
    from .types.common import RequestId
    from .types.kernel import CoreState
    from .types.settlement import RunResultProposal
    from .types.steps import StepResult


def open_record(state: CoreState, tag: RequestId) -> RequestRecord:
    """The open step ``tag``; a log that names another is not one this code wrote."""
    rows = state.records.find(tag)
    if len(rows) != 1 or not is_open(rows[0]):
        message = f"the log names step {tag.root}, which is not open"
        raise RecordRefusedError(RefusalReason.STEP_NOT_OPEN, message, tag)
    return rows[0]


def ended_by_core(state: CoreState, tag: RequestId) -> bool:
    """Whether core ended the step ``tag`` itself, so a record of its attempt is only history.

    A clock tick past a step's limit ends it in the ledger while the loop's attempt still runs.
    The step has its result; the attempt's end is recorded, and nothing it reports is applied. A
    step the log never held is not this: it is refused, as a log other code wrote.
    """
    rows = state.records.find(tag)
    if len(rows) > 1:
        message = f"the log names step {tag.root} more than once"
        raise RecordRefusedError(RefusalReason.STEP_NOT_OPEN, message, tag)
    if not rows:
        message = f"the log names step {tag.root}, which the run never held"
        raise RecordRefusedError(RefusalReason.STEP_NOT_OPEN, message, tag)
    return not is_open(rows[0])


def ended_unconcluded(state: CoreState) -> tuple[RequestRecord, ...]:
    """The steps core ended itself (at their limit) that still hold the run's closure.

    The kernel ends a step whose limit passes under a clock input, whatever its runner is doing.
    The step has its result, but a job or a turn it owns is released, and the run closes, only
    when the runner's end of the attempt concludes it (`concluded`), as it does any step.
    """
    return tuple(row for row in unreleased_records(state) if is_step(row) and not is_open(row))


def concluded(state: CoreState, tag: RequestId, reason: CancelReason) -> RequestObserved:
    """The conclusive observation that settles the blocked step ``tag`` the way any step ends."""
    (record,) = state.records.find(tag)
    return _concluded(record, Cancelled(reason=reason), state.run.now_at)


def epoch_opened(state: CoreState, epoch: int) -> CoreState:
    """``state`` in epoch ``epoch``, with every attempt of an earlier epoch ended.

    A run has one writer, so a record of a later epoch proves the process that ran the earlier
    epoch's attempts dead. They end without a result and count against the retry bound: the
    attempt may have run, and a step that kills its process every time must end as a typed failure.
    An attempt a parked run still held ended with the pause, and is not counted. Their steps go
    back to the state of a step no one has started, which is what a draining run waits for before
    it parks.

    The kernel's host epoch moves with the log's, and ``RunPark.epoch`` does not: it stays the
    epoch the operator paused in. A drain in progress keeps its authority in the new epoch (see
    ``drain_in_force``), so a Pause-now still cancels; when it parks, the park is older than the
    epoch it parks in, and the operator's Resume is admitted at once.
    """
    if epoch <= state.steps.epoch:
        return state
    origin = state.steps.epoch == 0
    parked = state.run.status is RunStatus.PARKED
    rows = []
    for row in state.steps.attempts:
        if ended_by_core(state, row.tag):
            continue
        # A park the process left with an attempt running (one that ignored the cancel) ended it:
        # the exit was the pause's, so it is not charged like a crash.
        spared = 1 if parked and not row.ended else 0
        rows.append(row.model_copy(update={"ended": True, "uncharged": row.uncharged + spared}))
        state = _returned(state, row.tag)
    state = _with_attempts(state, tuple(rows))
    if origin:
        state = _earlier_version_settled(state)
    recovery = state.records.recovery
    counted = epoch if origin else max(recovery.epoch, epoch)
    records = state.records.model_copy(
        update={"recovery": recovery.model_copy(update={"epoch": counted})}
    )
    park = state.run.park
    if origin and park is not None:
        park = park.model_copy(update={"epoch": min(park.epoch, epoch - 1)})
    run = state.run.model_copy(update={"park": park})
    steps = state.steps.model_copy(update={"epoch": epoch})
    return state.model_copy(update={"steps": steps, "records": records, "run": run})


def _with_attempts(state: CoreState, rows: tuple[StepAttempts, ...]) -> CoreState:
    return state.model_copy(update={"steps": state.steps.model_copy(update={"attempts": rows})})


def _earlier_version_settled(state: CoreState) -> CoreState:
    """``state``, a seed, with what an earlier version left in flight settled the way this one reads it.

    The log's first epoch starts a numbering of its own, so a seed's host epoch and the park it holds
    are older than any record that follows. The requests that settled unknown outcomes (an
    inspection, a cancel, a block) are not steps: nothing here sends them, and one left open would
    keep a pause from parking and a stop from ending. They lapse, as at their limit. A step in
    flight with no attempt in this log has no one running it, so it goes back to unstarted, to run
    again uncharged like one a pause interrupted. Its earlier attempt is the log's first, ended
    and uncharged, so the run that follows shows as a re-run and clears what the first left.
    """
    state = commands_lapsed(state)
    for record in state.records.records:
        if is_open(record) and state.steps.of(record.request_id) is None and _sent(record):
            first = StepAttempts(
                tag=record.request_id,
                started=1,
                ended=True,
                uncharged=1,
                deadline_at=record.until.at,
            )
            state = _returned(_with(state, record.request_id, first), record.request_id)
    return state


def _sent(record: RequestRecord) -> bool:
    """Whether a log of an earlier version shows the step was sent, so its work may exist.

    A step that was never dispatched has nothing to clear; one the earlier version dispatched, in
    flight now or sent again after a failure, may have work its runner has to find.
    """
    return (
        record.phase is not RequestPhase.PREPARED
        or record.dispatched_at is not None
        or record.dispatched_in is not None
    )


def commands_lapsed(state: CoreState) -> CoreState:
    """``state`` with every inspection, cancel and block that is still open lapsed.

    The kernel registers one when a limit passes under a clock input, for an executor to answer. A
    log has none: a step whose limit passed is ended by the record of its cancel, and one that
    must run again does so, so a command nothing sends would stay open and keep the run from
    parking, ending or coming to rest.
    """
    for record in state.records.records:
        if command_open(record):
            state = _replaced(state, record.model_copy(update={"phase": RequestPhase.BLOCKED}))
    return state


def started(
    state: CoreState, tag: RequestId, attempt: int, request: object, deadline: Until
) -> CoreState:
    """``state`` with attempt ``attempt`` of ``tag`` begun in the epoch the log is in."""
    record = open_record(state, tag)
    previous = state.steps.of(tag)
    expected = 1 if previous is None else previous.started + 1
    if attempt != expected or record.request != request or (previous and not previous.ended):
        message = f"the log starts attempt {attempt} of step {tag.root}, not its attempt {expected}"
        raise RecordRefusedError(RefusalReason.ATTEMPT_MISMATCH, message, tag)
    if record.phase is RequestPhase.PREPARED:
        record = record.model_copy(
            update={
                "phase": RequestPhase.DISPATCHED,
                "until": deadline,
                "dispatched_at": state.run.now_at,
            }
        )
    row = StepAttempts(
        tag=tag,
        started=attempt,
        uncharged=0 if previous is None else previous.uncharged,
        deadline_at=deadline.at if previous is None else previous.deadline_at,
    )
    return _with(_replaced(state, record), tag, row)


def interrupted(state: CoreState, tag: RequestId, attempt: int, why: Interruption) -> CoreState:
    """``state`` with the running attempt of ``tag`` ended by ``why``, charged only if it raised.

    The attempt is the step's latest even when core ended the step itself.
    """
    by_core = ended_by_core(state, tag)
    row = state.steps.of(tag)
    if row is None or row.started != attempt or row.ended:
        message = f"the log interrupts attempt {attempt} of step {tag.root}, which is not running"
        raise RecordRefusedError(RefusalReason.ATTEMPT_MISMATCH, message, tag)
    if by_core:
        return forgotten(state, tag)
    uncharged = row.uncharged + (0 if why.charged else 1)
    state = _returned(state, tag)
    return _with(state, tag, row.model_copy(update={"ended": True, "uncharged": uncharged}))


def _returned(state: CoreState, tag: RequestId) -> CoreState:
    """``state`` with the open step ``tag`` back to the state of one no one has started.

    Its attempt ended without a result, so nothing is owed an answer and nothing runs: the
    request waits to be sent again, unreported to its owner, as a drain that interrupts a request
    leaves it (``resent``).
    """
    record = open_record(state, tag)
    if record.phase is RequestPhase.PREPARED:
        return state
    return _replaced(state, unstarted(state, record))


def unstarted(state: CoreState, record: RequestRecord) -> RequestRecord:
    """``record``, an open step whose attempt ended without a result, as one no one has started.

    Its limit is the one its first attempt started with. A drain caps the request it cannot
    interrupt, and the cap belongs to the attempt in flight: it ends with it, whoever returns the
    step (the fold, on the record of the attempt's end, or the drain, which lets a job go at once).
    """
    again = resent(record, state.run.now_at, state.run.limits.reconciliation_bound)
    row = state.steps.of(record.request_id)
    if row is None or row.deadline_at is None:
        return again
    return again.model_copy(
        update={"until": Until(at=row.deadline_at, watches=record.until.watches)}
    )


def ended(state: CoreState, result: RunResultProposal) -> CoreState:
    """``state`` with the end of the run published, which the log records once the run is over."""
    if state.run.result is None or state.run.result != result:
        message = "the log ends a run with a result the run does not hold"
        raise RecordRefusedError(RefusalReason.RUN_NOT_OVER, message)
    if state.run.status is not RunStatus.TERMINAL:
        message = "the log ends a run that is not over"
        raise RecordRefusedError(RefusalReason.RUN_NOT_OVER, message)
    return state.model_copy(update={"steps": state.steps.model_copy(update={"ended": result})})


def end_lifted(state: CoreState) -> CoreState:
    """``state`` without the end of a run that holds no result: a resume reopened it.

    The end stands for the result it published, in the epoch the run was over. A run stopped again
    gets the same result, and its end is a new one the log has to record.
    """
    if state.steps.ended is None or state.run.result is not None:
        return state
    return state.model_copy(update={"steps": state.steps.model_copy(update={"ended": None})})


def reported_by(state: CoreState, tag: RequestId, attempt: int) -> None:
    """Refuse a finish that names an attempt other than the latest the step began.

    A step settled without an attempt (a limit that passed in the queue) names the attempt it had.
    """
    row = state.steps.of(tag)
    latest = 0 if row is None else row.started
    if attempt != latest:
        message = f"the log finishes attempt {attempt} of step {tag.root}, whose latest is {latest}"
        raise RecordRefusedError(RefusalReason.ATTEMPT_MISMATCH, message, tag)


def open_step(state: CoreState, tag: RequestId) -> OpenStep | None:
    """The step ``tag`` as the loop runs it, while it has no result; None once it has one."""
    rows = state.records.find(tag)
    if len(rows) != 1 or not is_open(rows[0]):
        return None
    attempts = state.steps.of(tag)
    started_ = 0 if attempts is None else attempts.started
    return OpenStep(step=rows[0].request, deadline_at=rows[0].until.at, started=started_)


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


def finish(
    state: CoreState, tag: RequestId, result: StepResult
) -> tuple[RequestObserved, tuple[OwnerEvent, ...]]:
    """The events the ledger and the owners take for the result of ``tag``, in the order to apply.

    A finished step brings the executor's report and its owner events; each may speak only for the
    request it answers, as the live shell checks. For a failure or cancellation core decided, core
    writes the conclusive observation itself and no owner event follows: the ledger derives the
    owner's from it.
    """
    record = open_record(state, tag)
    match result:
        case Finished():
            for fact in (result.observation, *result.owner_events):
                _speaks_for(record, fact)
            return result.report, (*result.owner_events, *_job_ended(record, result))
        case Failed() | Cancelled():
            return _concluded(record, result, state.run.now_at), ()


def _job_ended(record: RequestRecord, result: Finished) -> tuple[JobObserved, ...]:
    """The job observation of a submission whose own report ended its job, else none.

    A runner that carries a job to its end answers the submission with the job's terminal
    observation, evidence and facts: the report is the job's last observation, and no poll will
    bring another. Without this the job stays unobserved until a poll answers, and a run that is
    ending asks for no poll, so its closure would wait on a release nothing can prove. A runner
    that returns the job's observation beside its report has said it already.
    """
    observation = result.observation
    if (
        not isinstance(record.request, SubmitMeasurement)
        or not (observation.accepted and observation.terminal)
        or observation.resource_id is None
        or any(isinstance(event, JobObserved) for event in result.owner_events)
    ):
        return ()
    report = result.report
    return (
        JobObserved(
            progress=report.progress,
            resource_id=observation.resource_id,
            observation=observation,
            evidence=report.evidence,
            evaluation_result=report.evaluation_result,
            failure=report.measurement_failure,
        ),
    )


def _speaks_for(record: RequestRecord, fact: object) -> None:
    """Refuse a fact of the log that names another request, scope or admission."""
    request = record.request
    observation = fact if isinstance(fact, Observation) else getattr(fact, "observation", None)
    scope = getattr(fact, "scope", None)
    if (
        (scope is not None and scope != request.scope)
        or getattr(fact, "admission_id", request.admission_id) != request.admission_id
        or (
            observation is not None
            and (
                observation.scope != request.scope
                or observation.admission_id != request.admission_id
                or (isinstance(fact, Observation) and observation.request_id != record.request_id)
            )
        )
    ):
        message = f"the log finishes step {record.request_id.root} with a fact of another request"
        raise RecordRefusedError(RefusalReason.RESULT_MISMATCH, message, record.request_id)


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
