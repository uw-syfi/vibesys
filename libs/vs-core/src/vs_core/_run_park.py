"""Drain a run without settling its search or consuming unfinished measurements.

Park owns only admission and physical job release. Existing turn/checkpoint transitions
finish normally, and the ordinary persisted search and request ledger resume next epoch.

A park never leaves a running request behind. Both modes close admission and wait until every
request the run dispatched has recorded its outcome. Pause-now also cancels what can be
cancelled at once (turns, jobs). A pause may carry a drain deadline (``RunPark.deadline_at``,
from the event's ``drain_seconds``; Pause-now has the run's cleanup bound when it names none).
At the deadline the drain stops waiting: a request recovery would send again goes back to
PREPARED, interrupted and unreported (``resent``), and the next epoch runs it again; the rest is
cancelled and ends at its limit, a cleanup bound after the deadline.

The one exception is a request whose attempt the step loop runs (``_attempt_running``). A drain
that cancels does not wait for it: the loop cancels the attempt and records its interruption
when it can, and a call that cannot be stopped ends with the process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._recovery_unanswered import Inspection, settlement
from ._request_recovery import resent
from ._turn_permit import (
    cap_open_records,
    invocation_permit,
    permit_for_command,
    permit_for_request,
    scope_in_flight,
    turn_executing,
)
from .types.attempts import CloseAttemptScope, DiscardWorkspace, RetainRevision, SnapshotAndRetain
from .types.common import RequestId, RunStatus
from .types.evaluation import (
    CancelOwnedJob,
    CollectEvidence,
    InspectOwnedJob,
    ObserveOwnedJob,
    OwnedJob,
    SubmitMeasurement,
)
from .types.records import BlockRequest, RequestPhase, Scheduled
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
    if park is None or park.immediate or park.epoch != state.records.recovery.epoch:
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
    if _withdrawn(state, request):
        return False
    return (
        _request_bound(state, request) is not None
        or isinstance(request, _DRAIN_EFFECTS)
        or (
            isinstance(request, ObserveOwnedJob | InspectOwnedJob)
            and scope_in_flight(state.sessions, state.records, request.scope)
        )
    )


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


def pause_withdraws(state: CoreState, request: Request) -> bool:
    """Whether committed Pause now withdrew this effect's right to complete the epoch."""
    park = state.run.park
    return park is not None and park.immediate and _park_holds_back(state, park, request)


def _withdrawn(state: CoreState, request: Request) -> bool:
    """Whether Pause now, or a drain past its deadline, no longer lets this effect start."""
    park = state.run.park
    return park is not None and _immediate(state, park) and _park_holds_back(state, park, request)


def _park_holds_back(state: CoreState, park: RunPark, request: Request) -> bool:
    """Whether this epoch's park holds ``request`` back; what finishes a turn's work is left to end."""
    return state.records.recovery.epoch == park.epoch and not isinstance(request, _DRAIN_EFFECTS)


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
    # A submitted job keeps its request open until the measurement ends. The park
    # cancellation releases its physical job and deliberately preserves that request.
    return not (
        isinstance(record.request, SubmitMeasurement)
        and any(job.submission_id == record.request_id for job in state.evaluation.jobs)
    )


def _holds_park(state: CoreState, record: RequestRecord, *, immediate: bool) -> bool:
    """Whether this request keeps the drain from parking: it is unfinished, and the drain waits.

    A drain that cancels (a Pause now, or any pause past its deadline) does not wait for a request
    whose attempt the step loop runs: the loop cancels it, and a call that cannot be stopped
    ends with the process, which the next epoch's ``clear`` reaps. The kernel's own requests are
    waited for, so a park leaves none of them open.
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

    A request that cannot be sent again is cancelled once the deadline passes (a turn, a job) or
    left to its own limit; either way it ends by ``cleanup_bound`` after the deadline, as any
    request ends at its limit, and the loop then cancels its executor.
    """
    park = state.run.park
    if park is None or park.deadline_at is None or state.run.status != RunStatus.DRAINING:
        return state
    bound = park.deadline_at + state.run.limits.cleanup_bound
    return cap_open_records(
        state,
        lambda record: bound if _unfinished(state, record) and not _rerunnable(record) else None,
    )


def _interrupt_overdue(state: CoreState, park: RunPark) -> CoreState:
    """At the drain deadline, send the running requests recovery could send again back to PREPARED.

    The request is neither observed nor reported to its owner, and counts as no failure and no
    retry. The loop cancels its executor, whose cleanup runs, and the next epoch dispatches it
    again unchanged.
    """
    if not _past_deadline(state, park):
        return state
    reconciliation = state.run.limits.reconciliation_bound
    records = tuple(
        resent(row, state.run.now_at, reconciliation)
        if _unfinished(state, row) and _rerunnable(row)
        else row
        for row in state.records.records
    )
    if records == state.records.records:
        return state
    return state.model_copy(
        update={"records": state.records.model_copy(update={"records": records})}
    )


def advance(state: CoreState) -> tuple[CoreState, tuple[Scheduled, ...]]:
    """Request each live job's release once, then park after turns and their checkpoints drain."""
    park = state.run.park
    if state.run.status != RunStatus.DRAINING or park is None:
        return state, ()
    state = _interrupt_overdue(state, park)
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
        if job.terminal or job.resource_id is None:
            continue
        identity = RequestId(root=f"park:{park.control_id.root}:{job.resource_id.root}")
        held = state.records.record(identity)
        if held is None:
            source_id = job.submission_id if isinstance(job, OwnedJob) else job.request_id
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
        row.phase == RequestPhase.PREPARED and dispatch_allowed(state, row.request)
        for row in state.records.records
    ):
        return state, ()
    return state.model_copy(
        update={"run": state.run.model_copy(update={"status": RunStatus.PARKED})}
    ), ()
