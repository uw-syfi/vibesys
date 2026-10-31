"""Drain a run without settling its search or consuming unfinished measurements.

Park owns admission, and the physical release of the jobs an executor hosts. Existing
turn/checkpoint transitions finish normally, and the ordinary persisted search and request ledger
resume next epoch.

A park never leaves a running request behind, with two exceptions. A run the step loop hosts
(``_hosted_by_loop``) leaves its jobs alone: the request that watches a job goes back to
PREPARED, interrupted and unreported (``resent``), the job runs on, and the next epoch runs the
request again, where its runner finds the job and attaches to it. An executor's run still has its
jobs cancelled and their requests preserved, until the cutover removes the executor. And a request
whose attempt the step loop runs (``_attempt_running``) does not hold a drain that cancels: the
loop cancels the attempt and records its interruption when it can, and a call that cannot be
stopped ends with the process, whose attempt the next epoch ends as interrupted, not as a crash
(``epoch_opened``).

Both modes close admission and wait until every other request the run dispatched has recorded its
outcome. Pause-now also cancels what can be cancelled at once (turns, an executor's jobs). A pause
may carry a drain deadline (``RunPark.deadline_at``, from the event's ``drain_seconds``; Pause-now
has the run's cleanup bound when it names none). At the deadline the drain stops waiting: a request
recovery would send again goes back to PREPARED the same way; the rest is cancelled and ends at
its limit, a cleanup bound after the deadline. Time changes the log only through a record, and the
deadline passing writes none, so ``decide`` asks for the clock (``Tick``) and the fold parks the
drain on it: whether a run is parked never depends on an unrelated step ending later.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._proofs import request_dependency_status
from ._recovery_unanswered import Inspection, settlement
from ._scope_order import holds_back
from ._step_attempts import unstarted
from ._turn_permit import (
    cap_open_records,
    drain_in_force,
    invocation_permit,
    permit_for_command,
    permit_for_request,
    scope_in_flight,
    turn_executing,
)
from .types.attempts import CloseAttemptScope, DiscardWorkspace, RetainRevision, SnapshotAndRetain
from .types.common import DependencyStatus, LifecycleClass, RequestId, RunStatus
from .types.evaluation import (
    CancelOwnedJob,
    CollectEvidence,
    InspectOwnedJob,
    ObserveOwnedJob,
    OwnedJob,
    SubmitMeasurement,
)
from .types.records import BlockRequest, RequestPhase, Scheduled, request_lifecycle
from .types.sessions import (
    CancelTurn,
    CloseSession,
    DispatchTurn,
    EnsureSession,
    ResumeSessionTurn,
    SessionPhase,
    SnapshotAndRetainRun,
)
from .types.until import Until

if TYPE_CHECKING:
    from .types.common import InvocationRef, RunPark
    from .types.kernel import CoreState, RunControlEvent
    from .types.records import Request, RequestRecord
    from .types.sessions import Invocation
    from .types.strategy import Command
# These effects finish work already performed by a turn. Everything else waits for
# relaunch, including prepared turns, measurements, polls and queued attempts.
_DRAIN_EFFECTS = (
    SnapshotAndRetain,
    SnapshotAndRetainRun,
    RetainRevision,
    CloseSession,
    CancelTurn,
    DiscardWorkspace,
    CloseAttemptScope,
    BlockRequest,
    CancelOwnedJob,
    CollectEvidence,
)


def admitted_turns(state: CoreState) -> tuple[InvocationRef, ...]:
    """Capture physical ancestors once; only their bounded logical successors may drain."""
    return tuple(
        invocation.invocation
        for invocation in state.sessions.invocations
        if any(
            isinstance(row.request, DispatchTurn | ResumeSessionTurn)
            and row.request.turn == invocation.turn
            and row.request.scope == invocation.scope
            and row.phase != RequestPhase.PREPARED
            and row.dispatched_in in (None, state.records.recovery.epoch)
            for row in state.records.records
        )
    )


def drain_bound(state: CoreState, command: Command) -> float | None:
    """The deadline of a non-immediate Pause, for a successor of a turn it admitted.

    A running turn's permit caps that turn's deadlines. It does not admit drain
    successors or move the run to draining: without this Pause, a successor is
    admitted by the running run.
    """
    park = state.run.park
    if park is None or park.immediate or not drain_in_force(state.run, state.records):
        return None
    permit = permit_for_command(state, command)
    return None if permit is None else permit.deadline_at


def _request_bound(state: CoreState, request: Request) -> float | None:
    permit = permit_for_request(state, request)
    return None if permit is None else permit.deadline_at


def dispatch_allowed(state: CoreState, request: Request) -> bool:
    """Whether the park drain permits this effect; the authorization boundary owns the gate."""
    if state.run.status == RunStatus.PARKED:
        return state.run.park is not None and state.records.recovery.epoch > state.run.park.epoch
    if state.run.status != RunStatus.DRAINING:
        return True
    if _pause_now_holds_back(state, request):
        return False
    return (
        _request_bound(state, request) is not None
        or isinstance(request, _DRAIN_EFFECTS)
        or (
            isinstance(request, ObserveOwnedJob | InspectOwnedJob)
            and scope_in_flight(state.sessions, state.records, request.scope)
        )
    )


def starts(state: CoreState, record: RequestRecord, dependencies: DependencyStatus) -> bool:
    """Whether the run lets the prepared step ``record`` start.

    It is the one question `decide` and the drain of a pause both ask.

    Its dependencies are met, the run's status admits it (a draining run starts only the work the
    drain lets through), and no step of its scope must end first. A drain parks when nothing
    starts, so a step the rule holds back can never keep a pause from parking.
    """
    return (
        dependencies is not DependencyStatus.PENDING
        and _admits(state, record.request)
        and not holds_back(state, record)
    )


def _admits(state: CoreState, request: Request) -> bool:
    """Whether the run's status lets a step start: a paused run starts none but drain work."""
    match state.run.status:
        case RunStatus.RUNNING | RunStatus.CLOSING:
            return True
        case RunStatus.DRAINING:
            return dispatch_allowed(state, request)
        case _:
            return False


def drain_deadline(event: RunControlEvent, now_at: float, cleanup_bound: float) -> float | None:
    """When the drain a pause starts stops waiting; None for a pause that waits for its turns.

    The pause's own ``drain_seconds`` says; a Pause-now that names none gets the cleanup bound.
    """
    if event.drain_seconds is not None:
        return now_at + event.drain_seconds
    return now_at + cleanup_bound if event.control.action == "pause_now" else None


def _immediate(state: CoreState, park: RunPark) -> bool:
    """Whether the drain cancels at once: a Pause-now, or any pause past its deadline."""
    return park.immediate or _past_deadline(state, park)


def _past_deadline(state: CoreState, park: RunPark) -> bool:
    return park.deadline_at is not None and state.run.now_at >= park.deadline_at


def _pause_now_holds_back(state: CoreState, request: Request) -> bool:
    """Whether a committed Pause-now, or a pause past its deadline, lets this effect start no more."""
    park = state.run.park
    return (
        park is not None
        and drain_in_force(state.run, state.records)
        and _immediate(state, park)
        and not isinstance(request, _DRAIN_EFFECTS)
    )


def _releases_a_job(state: CoreState, request: Request) -> bool:
    """Whether the drain lets go of the request that watches a job, which any pause does at once."""
    park = state.run.park
    return (
        park is not None
        and state.run.status == RunStatus.DRAINING
        and state.records.recovery.epoch == park.epoch
        and request_lifecycle(request) is LifecycleClass.OWNED_JOB
    )


def pause_cancels(state: CoreState, request: Request) -> bool:
    """Whether the drain of a pause cancels this running request now.

    A Pause-now does from the start, any pause does once its drain deadline has passed, and a
    pause lets go of the request that watches a job at once: the job is left running. The effects
    that finish work a turn already did (a snapshot, a cleanup) are left to end.
    """
    return _pause_now_holds_back(state, request) or _releases_a_job(state, request)


def _unfinished(state: CoreState, record: RequestRecord) -> bool:
    if record.phase not in (RequestPhase.DISPATCHED, RequestPhase.RECONCILING):
        return False
    if isinstance(record.request, EnsureSession) and record.observation is not None:
        if not record.observation.accepted or record.observation.resource_id is None:
            return True
        return not any(
            session.resource_id == record.observation.resource_id
            for session in state.sessions.sessions
        )
    # A job an executor watches keeps its request open until the measurement ends: the park
    # cancellation releases its physical job and deliberately preserves that request. The step
    # loop's request does not: the loop interrupts it and the job runs on (``_interrupt``).
    return not (
        isinstance(record.request, SubmitMeasurement)
        and not _hosted_by_loop(state)
        and any(job.submission_id == record.request_id for job in state.evaluation.jobs)
    )


def _holds_park(state: CoreState, record: RequestRecord, *, immediate: bool) -> bool:
    """Whether this request keeps the drain from parking: it is unfinished, and the drain waits.

    A drain that cancels (a Pause now, or any pause past its deadline) does not wait for a request
    whose attempt the step loop runs: the loop cancels it, and a call that cannot be stopped
    ends with the process: the next epoch ends its attempt as interrupted (``epoch_opened``) and
    its ``clear`` reaps what it left. The kernel's own requests are waited for, so a park leaves none
    of them open.
    """
    return _unfinished(state, record) and not (immediate and _attempt_running(state, record))


def _attempt_running(state: CoreState, record: RequestRecord) -> bool:
    """Whether the log holds an attempt of this step that no record has ended."""
    attempts = state.steps.of(record.request_id)
    return attempts is not None and not attempts.ended


def _turns_in_flight(state: CoreState) -> bool:
    """A prepared dispatch marks its invocation executing before I/O actually starts."""
    return any(
        turn_executing(turn, state.records)
        or (
            turn.phase is SessionPhase.SUSPENDED
            and invocation_permit(state, turn.invocation) is not None
        )
        for turn in state.sessions.invocations
    )


def _released_or_bounded(state: CoreState, record: RequestRecord | None) -> bool:
    if record is None:
        return False
    observation = record.observation
    return (observation is not None and observation.accepted and observation.released) or (
        record.phase is RequestPhase.BLOCKED and state.run.now_at >= record.until.at
    )


def _dispatched(state: CoreState, invocation: Invocation) -> bool:
    """Whether a dispatch of the turn was authorized: otherwise nothing runs that a cancel could stop.

    Cancelling a turn that never began would close its fence, and the next epoch could not
    dispatch it.
    """
    return any(
        isinstance(row.request, DispatchTurn | ResumeSessionTurn)
        and row.request.turn == invocation.turn
        and row.request.scope == invocation.scope
        and row.phase != RequestPhase.PREPARED
        for row in state.records.records
    )


def _cancel_turns(state: CoreState, park: RunPark) -> tuple[list[Scheduled], bool]:
    if not _immediate(state, park):
        return [], True
    pending: list[Scheduled] = []
    released = True
    for invocation in state.sessions.invocations:
        if invocation.phase is SessionPhase.TERMINAL or not _dispatched(state, invocation):
            continue
        identity = RequestId(
            root=f"pause-now:{park.control_id.root}:{invocation.invocation.invocation_id.root}"
        )
        held = state.records.record(identity)
        if held is None:
            source = next(
                (
                    row.request
                    for row in state.records.records
                    if isinstance(row.request, DispatchTurn | ResumeSessionTurn)
                    and row.request.turn == invocation.turn
                    and row.request.scope == invocation.scope
                ),
                None,
            )
            pending.append(
                Scheduled(
                    request=CancelTurn(
                        request_id=identity,
                        scope=invocation.scope,
                        admission_id=None if source is None else source.admission_id,
                        invocation=invocation.invocation,
                    ),
                    until=Until(at=state.run.now_at + state.run.limits.cleanup_bound),
                )
            )
        released = released and _released_or_bounded(state, held)
    return pending, released


def _rerunnable(record: RequestRecord) -> bool:
    """Whether recovery would send this open request again, which is when it may be interrupted."""
    return (
        settlement(record, inspection=Inspection.ANSWERED, owes_release=False, resendable=True)
        == "resend"
    )


def cap_to_drain_deadline(state: CoreState) -> CoreState:
    """Give the running requests a drain's deadline cannot interrupt a cleanup bound past it.

    A request that cannot be sent again is cancelled once the deadline passes (a turn) or left to
    its own limit; either way it ends by ``cleanup_bound`` after the deadline, as any request ends
    at its limit, and the loop then cancels its executor. In a run the step loop hosts a job's
    request is not among them: the drain returns it at once, and the cap would outlive the attempt.
    """
    park = state.run.park
    if park is None or park.deadline_at is None or state.run.status != RunStatus.DRAINING:
        return state
    bound = park.deadline_at + state.run.limits.cleanup_bound
    return cap_open_records(
        state,
        lambda record: bound if _unfinished(state, record) and not _rerunnable(record) else None,
    )


def _hosted_by_loop(state: CoreState) -> bool:
    """Whether the step loop runs this run: its log has opened an epoch.

    An executor's run has none. Its jobs are released by the park's cancellation instead, until
    the cutover replaces the executor; the loop's runs leave a job alone.
    """
    return state.steps.epoch > 0


def _holds_a_job(record: RequestRecord) -> bool:
    """Whether the request is the one that watches a job, which a pause releases at once."""
    return request_lifecycle(record.request) is LifecycleClass.OWNED_JOB


def _interrupt(state: CoreState, park: RunPark) -> CoreState:
    """Send the running requests a drain no longer waits for back to PREPARED.

    A job's request goes at once, a pause leaving the job alone; the rest recovery could send
    again go at the drain deadline. The request is neither observed nor reported to its owner, and
    counts as no failure and no retry. The loop cancels its executor, whose cleanup leaves a job
    running, and the next epoch dispatches the request again unchanged.
    """
    overdue = _past_deadline(state, park)
    records = tuple(
        unstarted(state, row)
        if _unfinished(state, row)
        and ((_holds_a_job(row) and _hosted_by_loop(state)) or (overdue and _rerunnable(row)))
        else row
        for row in state.records.records
    )
    if records == state.records.records:
        return state
    return state.model_copy(
        update={"records": state.records.model_copy(update={"records": records})}
    )


def advance(state: CoreState) -> tuple[CoreState, tuple[Scheduled, ...]]:
    """Release what a drain no longer waits for, then park after turns and their checkpoints drain."""
    park = state.run.park
    if state.run.status != RunStatus.DRAINING or park is None:
        return state, ()
    state = _interrupt(state, park)
    immediate = _immediate(state, park)
    # Polls/submissions already authorized must commit before their jobs are cancelled.
    if not immediate and any(
        _unfinished(state, record)
        and isinstance(record.request, SubmitMeasurement | ObserveOwnedJob | InspectOwnedJob)
        for record in state.records.records
    ):
        return state, ()
    pending, released = _cancel_turns(state, park)
    for job in (*state.evaluation.jobs, *state.evaluation.registered_jobs):
        source_id = job.submission_id if isinstance(job, OwnedJob) else job.request_id
        if job.terminal or job.resource_id is None or _hosted_by_loop(state):
            continue  # the step loop hosts this run, and a pause leaves its jobs alone
        identity = RequestId(root=f"park:{park.control_id.root}:{job.resource_id.root}")
        held = state.records.record(identity)
        if held is None:
            source = state.records.record(source_id)
            request = CancelOwnedJob(
                request_id=identity,
                scope=job.scope,
                admission_id=None if source is None else source.request.admission_id,
                resource_id=job.resource_id,
                for_park=True,
                immediate=immediate,
            )
            pending.append(
                Scheduled(
                    request=request,
                    until=Until(
                        at=(
                            state.run.now_at + state.run.limits.cleanup_bound
                            if immediate
                            else max(
                                state.run.now_at + state.run.limits.cleanup_bound,
                                state.run.now_at if source is None else source.until.at,
                            )
                        )
                    ),
                )
            )
        observation = None if held is None else held.observation
        released = released and (
            _released_or_bounded(state, held)
            if immediate
            else observation is not None and observation.accepted and observation.released
        )
    if pending:
        return state, tuple(pending)
    if (
        not released
        or any(_holds_park(state, row, immediate=immediate) for row in state.records.records)
        or (not immediate and _turns_in_flight(state))
    ):
        return state, ()
    if not immediate and any(
        row.phase == RequestPhase.PREPARED
        and starts(
            state,
            row,
            request_dependency_status(row.request, state.run.receipts, state.records),
        )
        for row in state.records.records
    ):
        return state, ()
    return state.model_copy(
        update={"run": state.run.model_copy(update={"status": RunStatus.PARKED})}
    ), ()
