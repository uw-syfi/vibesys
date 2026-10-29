"""Drain a run without settling its search or consuming unfinished measurements.

Park owns only admission and physical job release. Existing turn/checkpoint transitions
finish normally, and the ordinary persisted search and request ledger resume next epoch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._turn_permit import invocation_permit, permit_for_command, permit_for_request
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
    from .types.common import InvocationRef, Scope
    from .types.kernel import CoreState
    from .types.records import RecordsState, Request, RequestRecord
    from .types.sessions import Invocation, SessionsState
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
    if pause_withdraws(state, request):
        return False
    return (
        _request_bound(state, request) is not None
        or isinstance(request, _DRAIN_EFFECTS)
        or (
            isinstance(request, ObserveOwnedJob | InspectOwnedJob)
            and scope_in_flight(state.sessions, state.records, request.scope)
        )
    )


def pause_withdraws(state: CoreState, request: Request) -> bool:
    """Whether committed Pause now withdrew this effect's right to complete the epoch."""
    return (
        state.run.park is not None
        and state.records.recovery.epoch == state.run.park.epoch
        and state.run.park.immediate
        and not isinstance(request, _DRAIN_EFFECTS)
    )


def scope_in_flight(sessions: SessionsState, records: RecordsState, scope: Scope) -> bool:
    """An admitted turn of this owner has crossed core's dispatch boundary."""
    return any(turn.scope == scope and _executing(turn, records) for turn in sessions.invocations)


def _executing(turn: Invocation, records: RecordsState) -> bool:
    return turn.phase in (SessionPhase.EXECUTING, SessionPhase.UNKNOWN) and any(
        isinstance(row.request, DispatchTurn | ResumeSessionTurn)
        and row.request.turn == turn.turn
        and row.request.scope == turn.scope
        and row.phase != RequestPhase.PREPARED
        for row in records.records
    )


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


def _turns_in_flight(state: CoreState) -> bool:
    """A prepared dispatch marks its invocation executing before I/O actually starts."""
    return any(
        _executing(turn, state.records)
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


def _cancel_turns(state: CoreState) -> tuple[list[Scheduled], bool]:
    park = state.run.park
    if park is None or not park.immediate:
        return [], True
    pending: list[Scheduled] = []
    released = True
    for invocation in state.sessions.invocations:
        if invocation.phase is SessionPhase.TERMINAL:
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


def advance(state: CoreState) -> tuple[CoreState, tuple[Scheduled, ...]]:
    """Request each live job's release once, then park after turns and their checkpoints drain."""
    park = state.run.park
    if state.run.status != RunStatus.DRAINING or park is None:
        return state, ()
    # Polls/submissions already authorized must commit before their jobs are cancelled.
    if not park.immediate and any(
        _unfinished(state, record)
        and isinstance(record.request, SubmitMeasurement | ObserveOwnedJob | InspectOwnedJob)
        for record in state.records.records
    ):
        return state, ()
    pending, released = _cancel_turns(state)
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
                immediate=park.immediate,
            )
            pending.append(
                Scheduled(
                    request=request,
                    until=Until(
                        at=(
                            state.run.now_at + state.run.limits.cleanup_bound
                            if park.immediate
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
            if park.immediate
            else observation is not None and observation.accepted and observation.released
        )
    if pending:
        return state, tuple(pending)
    if not released or (
        not park.immediate
        and (
            any(_unfinished(state, row) for row in state.records.records) or _turns_in_flight(state)
        )
    ):
        return state, ()
    if not park.immediate and any(
        row.phase == RequestPhase.PREPARED and dispatch_allowed(state, row.request)
        for row in state.records.records
    ):
        return state, ()
    return state.model_copy(
        update={"run": state.run.model_copy(update={"status": RunStatus.PARKED})}
    ), ()
