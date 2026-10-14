"""Every wait names what will end it: a pure check over core state, no state change.

Core waits in several places: a record in flight, a session acquiring, executing or
closing, a recovery check, an owned job. Each one is ended by something that already exists:
a request the shell still holds, an event committed but not yet applied (the outbox), or a
timer. A wait with none of these is an orphan: nothing will ever end it, so the run would sit
until its deadline. ``orphan_waits`` reports it at the step that created it.

One derivation, ``producers``, says what can move each waiting entity (``subjects``): the
waits report it, and every resolver below ends exactly the entities it finds nothing for, so
a wait and its resolver cannot disagree about whether something will end it.

Reporting is not enough, so every kind also declares in ``RESOLUTIONS`` what core does when
a wait of that kind would be an orphan: fail the waiter with a named reason, retry with a
bound and then fail, or (a known gap, listed as such) halt the run loudly. The step runs
those resolutions at quiescence, so a generated world never ends a step with an orphan.

A phase that waits on the strategy or the operator (idle, checkpointed, parked) is not
listed: the strategy's next proposal or the run deadline ends it. Each phase enum is matched
exhaustively below, so a new phase fails the type check and ``test_waits`` until someone
says whether it waits and what ends the wait.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, assert_never

from ._dispatch_episode import episode_ended
from ._proofs import blocked_unnamed, dependency_failed
from .types.attempts import AttemptPhase, SnapshotAndRetain
from .types.common import (
    InvocationRef,
    LifecycleClass,
    ObservationStatus,
    RunStatus,
    SessionId,
    Value,
)
from .types.evaluation import (
    ContinuationPhase,
    InspectOwnedJob,
    ObserveOwnedJob,
)
from .types.records import (
    ExecuteRegisteredOperation,
    InspectRequest,
    RecoveryPhase,
    RequestPhase,
)
from .types.sessions import (
    CloseSession,
    DispatchTurn,
    EnsureSession,
    InspectTurn,
    ResumeSessionTurn,
    SessionPhase,
    SnapshotAndRetainRun,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from .types.common import RequestId
    from .types.kernel import CoreEvent, CoreState
    from .types.records import Request, RequestRecord
    from .types.sessions import Invocation, SessionsState


class WaitKind(StrEnum):
    """What is waiting."""

    SESSION = "session"
    RECOVERY_CHECK = "recovery-check"
    OWNED_JOB = "owned-job"
    RELEASE_DEPENDENCY = "release-dependency"
    YIELD_PUBLICATION = "yield-publication"
    INTERRUPT = "interrupt"
    REFUND = "refund"
    DISPATCH = "dispatch"
    RETRY = "retry"
    TURN_ACQUISITION = "turn-acquisition"
    BLOCKED_RECORD = "blocked-record"


class Producer(StrEnum):
    """What will end a wait."""

    PENDING_REQUEST = "pending-request"
    OUTBOX_EVENT = "outbox-event"
    TIMER = "timer"


class Outcome(StrEnum):
    """What core does with a wait that nothing can end, declared once per wait kind."""

    FAILS_WITH_REASON = "fails-with-reason"
    """The step ends the wait with a failure event that names the reason (see ``RESOLUTIONS``)."""

    RETRIES_THEN_FAILS = "retries-then-fails"
    """Core retries the request a bounded number of times with backoff, then fails it."""

    HALTS_THE_RUN = "halts-the-run"
    """No resolver exists yet: the shell halts loudly with the orphan named (a known gap)."""


class Resolution(Value):
    """How one kind of wait is ended when nothing live can end it."""

    outcome: Outcome
    reason: str
    """The named reason the strategy or operator sees, or what the gap is for a halt."""


class OpenWait(Value):
    """One waiting entity and the producer that will end its wait, or None for an orphan."""

    waiter: WaitKind
    subject: str
    producer: Producer | None = None


RESOLUTIONS: Mapping[WaitKind, Resolution] = MappingProxyType(
    {
        WaitKind.YIELD_PUBLICATION: Resolution(
            outcome=Outcome.FAILS_WITH_REASON,
            reason="YieldPublicationFailed(checkpoint_declined | no_checkpoint | unpublishable)",
        ),
        WaitKind.INTERRUPT: Resolution(
            outcome=Outcome.FAILS_WITH_REASON,
            reason="InterruptFailed(checkpoint_declined | no_checkpoint | unpublishable)",
        ),
        WaitKind.REFUND: Resolution(
            outcome=Outcome.FAILS_WITH_REASON, reason="InterruptFailed(refund_unapplied)"
        ),
        WaitKind.DISPATCH: Resolution(
            outcome=Outcome.FAILS_WITH_REASON,
            reason="the request is cancelled unsent; its owner hears a conclusive failure",
        ),
        WaitKind.RETRY: Resolution(
            outcome=Outcome.RETRIES_THEN_FAILS,
            reason="after Limits.request_retries retries the request ends failed, not accepted",
        ),
        WaitKind.TURN_ACQUISITION: Resolution(
            outcome=Outcome.FAILS_WITH_REASON,
            reason="the turn ends cancelled with TurnResult.detail naming that nothing carried "
            "its acquisition forward; its decision completes cancelled",
        ),
        WaitKind.BLOCKED_RECORD: Resolution(
            outcome=Outcome.FAILS_WITH_REASON,
            reason="the record ends failed (diagnostic names the close), keeping the ownership "
            "facts it reported; the strategy already heard RequestBlocked",
        ),
        WaitKind.SESSION: Resolution(
            outcome=Outcome.HALTS_THE_RUN,
            reason="a record blocked by its reconciliation bound is not routed to the session",
        ),
        WaitKind.RECOVERY_CHECK: Resolution(
            outcome=Outcome.HALTS_THE_RUN,
            reason="a recovery check whose inspection was lost has no deadline of its own",
        ),
        WaitKind.OWNED_JOB: Resolution(
            outcome=Outcome.HALTS_THE_RUN,
            reason="a job with no poll live and no timer armed (a blocked poll is polled anew, but a "
            "poll lost some other way is not routed to the job)",
        ),
        WaitKind.RELEASE_DEPENDENCY: Resolution(
            outcome=Outcome.HALTS_THE_RUN,
            reason="a release recorded after the attempt stopped listening is not re-announced",
        ),
    }
)
"""The outcome of every wait kind that has no live resolver. A new kind must declare one."""


def record_waits(phase: RequestPhase) -> bool:
    """Whether a record in this phase waits on the shell to execute or inspect its request."""
    match phase:
        case RequestPhase.PREPARED | RequestPhase.DISPATCHED | RequestPhase.RECONCILING:
            return True
        case RequestPhase.COMPLETED | RequestPhase.BLOCKED:
            return False
        case _:
            assert_never(phase)


def session_waits(phase: SessionPhase) -> bool:
    """Whether a session in this phase waits on a request's result."""
    match phase:
        case SessionPhase.ACQUIRING | SessionPhase.EXECUTING | SessionPhase.CLOSING:
            return True
        case (
            SessionPhase.IDLE
            | SessionPhase.CHECKPOINTED
            | SessionPhase.SUSPENDED
            | SessionPhase.TERMINAL
            | SessionPhase.UNKNOWN
        ):
            return False
        case _:
            assert_never(phase)


def recovery_waits(phase: RecoveryPhase) -> bool:
    """Whether a recovery barrier in this phase waits on inspection results.

    ``REQUIRED`` waits for the host to start recovery, which is the host's own act.
    """
    match phase:
        case RecoveryPhase.RECOVERING:
            return True
        case RecoveryPhase.REQUIRED | RecoveryPhase.READY | RecoveryPhase.BLOCKED:
            return False
        case _:
            assert_never(phase)


def continuation_waits(phase: ContinuationPhase) -> bool:
    """Whether a continuation in this phase waits on a request rather than the strategy.

    A continuation waits on its jobs (named by the owned-job waits) or on the strategy's
    resume decision, never directly on a request of its own.
    """
    match phase:
        case (
            ContinuationPhase.WAITING
            | ContinuationPhase.AUTHORIZED
            | ContinuationPhase.RESUMED
            | ContinuationPhase.PARKED
            | ContinuationPhase.CANCELLED
            | ContinuationPhase.REOPENING
            | ContinuationPhase.BLOCKED
        ):
            return False
        case _:
            assert_never(phase)


type WaitingPhase = RequestPhase | SessionPhase | RecoveryPhase | ContinuationPhase


def phase_waits(phase: WaitingPhase) -> bool:
    """Whether an entity in *phase* waits on a request, event or timer to end the phase."""
    if isinstance(phase, RequestPhase):
        return record_waits(phase)
    if isinstance(phase, SessionPhase):
        return session_waits(phase)
    if isinstance(phase, RecoveryPhase):
        return recovery_waits(phase)
    return continuation_waits(phase)


def _outbox_requests(outbox: Iterable[CoreEvent]) -> frozenset[RequestId]:
    """The requests whose observation is committed and waiting to be applied."""
    found: set[RequestId] = set()
    for event in outbox:
        observation = getattr(event, "observation", None)
        request_id = getattr(observation, "request_id", None)
        if request_id is not None:
            found.add(request_id)
    return frozenset(found)


def _concerns(request: Request, identity: SessionId, lifecycle: LifecycleClass | None) -> bool:
    """Whether *request* acquires, runs or closes a turn or lease of the session *identity*."""
    if isinstance(request, EnsureSession):
        return request.spec.session_id == identity
    if isinstance(request, CloseSession):
        return request.session_id == identity
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        return request.turn.session.session_id == identity
    if isinstance(request, ExecuteRegisteredOperation):
        return lifecycle is None or lifecycle == LifecycleClass.SESSION_TURN
    # Cancelling, inspecting or checkpointing a turn names the invocation it is about.
    invocation = getattr(request, "invocation", None)
    return isinstance(invocation, InvocationRef) and invocation.session_id == identity


class WaitSubject(Value):
    """One waiting entity: what kind of wait it is and the identity ``OpenWait.subject`` names."""

    kind: WaitKind
    identity: str


#: Producers from the one that fires last to the one that fires first: a committed event is
#: applied at the next step, a request is answered when the shell hears back, and a timer
#: fires only once the clock reaches it. A wait several producers can move reports the first
#: listed here; a wait that needs every one of several parties reports the last to fire.
_SETTLES_LAST = (Producer.TIMER, Producer.PENDING_REQUEST, Producer.OUTBOX_EVENT)


def _reported(found: frozenset[Producer]) -> Producer | None:
    return next((producer for producer in _SETTLES_LAST if producer in found), None)


def _from_records(
    related: Sequence[RequestRecord],
    outbox: frozenset[RequestId],
    *,
    spent: RequestId | None = None,
) -> frozenset[Producer]:
    """What *related* records can still produce: a request in flight, or a committed answer.

    *spent* names a record that stays open without producing anything more (a job's answered
    submission): it does not count as in flight, though an answer of it in the outbox does.
    """
    found: set[Producer] = set()
    if any(record_waits(row.phase) and row.request_id != spent for row in related):
        found.add(Producer.PENDING_REQUEST)
    if any(row.request_id in outbox for row in related):
        found.add(Producer.OUTBOX_EVENT)
    return frozenset(found)


def _with_successors(
    records: Sequence[RequestRecord], related: list[RequestRecord]
) -> list[RequestRecord]:
    """``related`` and the live requests that inspect, block or cancel one of them.

    Recovery asks about a request a dead host left behind (and blocks it once its limit passed)
    before the resource it serves moves on: a job is polled again, a session carried forward.
    While that inspection is in flight it is what produces the waited-on fact.
    """
    return [
        *related,
        *(row for row in records if any(_addresses(row, item) for item in related)),
    ]


def _session_producers(
    records: Sequence[RequestRecord], outbox: frozenset[RequestId], session: SessionId
) -> frozenset[Producer]:
    related = [row for row in records if _concerns(row.request, session, row.lifecycle)]
    return _from_records(_with_successors(records, related), outbox)


def _acquiring(sessions: SessionsState) -> tuple[Invocation, ...]:
    """The turns still acquiring: requested and charged or not, but with no result yet."""
    return tuple(
        row
        for row in sessions.invocations
        if row.phase == SessionPhase.ACQUIRING and row.observation is None
    )


def _acquisition_producers(
    sessions: SessionsState,
    records: Sequence[RequestRecord],
    in_flight: Sequence[Request],
    outbox: frozenset[RequestId],
    invocation: InvocationRef,
) -> frozenset[Producer]:
    """What can carry an acquiring turn on: a request about its session, or an interruption.

    A turn acquires through its session's lease request, its input reservation and its
    dispatch, so any request about its session, live, in flight, or inspected, carries it. A
    replacement turn also waits for the interruption of its predecessor, whose own waits are
    ended first (see ``stalled_checkpoint_waits``).
    """
    carried = any(_concerns(request, invocation.session_id, None) for request in in_flight) or any(
        claim.invocation.session_id == invocation.session_id
        and claim.phase in ("pending", "draining", "checkpointed")
        for claim in sessions.interrupts
    )
    found = _session_producers(records, outbox, invocation.session_id)
    return found | {Producer.PENDING_REQUEST} if carried else found


def stalled_acquisitions(
    sessions: SessionsState, records: Sequence[RequestRecord], in_flight: Sequence[Request] = ()
) -> tuple[InvocationRef, ...]:
    """The acquiring turns that nothing can carry forward: each one's wait is an orphan.

    A turn acquiring without a producer, for example because its charge was never granted,
    would stall the run, so the step ends it (``AcquisitionStalled``).
    """
    return tuple(
        row.invocation
        for row in _acquiring(sessions)
        if not _acquisition_producers(sessions, records, in_flight, frozenset(), row.invocation)
    )


def _recovery_producers(
    core: CoreState, outbox: frozenset[RequestId], target: RequestId
) -> frozenset[Producer]:
    """A pending check is re-resolved by an answer about its target, or by its left children.

    Besides the target and the check's own inspection, any live inspection of the target
    re-resolves the check when it answers: the deadline's, or a child inspection of a
    resource the target left behind.
    """
    live = [row for row in core.records.records if record_waits(row.phase)]
    check = next(row for row in core.records.recovery.checks if row.target == target)
    if any(
        row.request_id in (target, check.inspection)
        or (isinstance(row.request, InspectRequest) and row.request.target == target)
        for row in live
    ):
        return frozenset({Producer.PENDING_REQUEST})
    return _children_producers(core, outbox, target)


def _children_producers(
    core: CoreState, outbox: frozenset[RequestId], target: RequestId
) -> frozenset[Producer]:
    """What settles every child resource *target* left, when each is a job its ledger tracks.

    The job ledger is the other party that can settle such a child: once it observes the job
    ended, recovery hands the child to it and re-resolves the check. So each child waits on
    its job's own wait, over both ledgers; one child no job answers for, or a job nothing will
    move, leaves the check an orphan. The check ends only once every child has settled, so
    it is the slowest child's producer that ends it, whatever order the children came in.
    """
    children = [child for child in core.records.children if target in child.source_requests]
    jobs = {
        (job.resource_id, job.scope): job.resource_id.root
        for job in core.evaluation.jobs
        if not job.terminal
    } | {
        (job.resource_id, job.scope): job.request_id.root
        for job in core.evaluation.registered_jobs
        if not job.terminal and job.resource_id is not None
    }
    found: list[frozenset[Producer]] = [
        _job_producers(core, outbox, subject)
        if (subject := jobs.get((child.resource_id, child.scope))) is not None
        else frozenset()
        for child in children
    ]
    if not found or frozenset() in found:
        return frozenset()
    union = frozenset().union(*found)
    return frozenset({next(producer for producer in _SETTLES_LAST if producer in union)})


def _observes(record: RequestRecord, resource: object) -> bool:
    request = record.request
    return isinstance(request, ObserveOwnedJob | InspectOwnedJob) and (
        request.resource_id == resource
    )


def _job_producers(
    core: CoreState, outbox: frozenset[RequestId], identity: str
) -> frozenset[Producer]:
    """The producers of a live job's next observation: its poll timer, a poll, or an answer.

    A submission's own record stays open until the job ends, so once the submission has
    been answered it no longer produces anything: only a poll in flight, a timer or an
    outbox event does.
    """
    for job in core.evaluation.jobs:
        if not job.terminal and job.resource_id.root == identity:
            source, resource = job.submission_id, job.resource_id
            break
    else:
        registered = next(
            row
            for row in core.evaluation.registered_jobs
            if not row.terminal and row.request_id.root == identity
        )
        job, source, resource = registered, registered.request_id, registered.resource_id
    related = _with_successors(
        core.records.records,
        [
            row
            for row in core.records.records
            if row.request_id == source or (resource is not None and _observes(row, resource))
        ],
    )
    found = _from_records(related, outbox, spent=source if job.observation is not None else None)
    return found | {Producer.TIMER} if job.pacing.next_at is not None else found


def _checkpoint_records(
    records: Iterable[RequestRecord], invocation: InvocationRef
) -> list[RequestRecord]:
    """The records that can still end a yield: its checkpoint, or the inspection of its turn.

    An inspection can end it because the turn's acceptance is what lets the checkpoint be
    requested. Both are about exactly this invocation.
    """
    return [
        row
        for row in records
        if isinstance(row.request, SnapshotAndRetain | SnapshotAndRetainRun | InspectTurn)
        and row.request.invocation == invocation
    ]


def _checkpoint_producers(
    records: Sequence[RequestRecord],
    in_flight: Sequence[Request],
    outbox: frozenset[RequestId],
    invocation: InvocationRef,
) -> frozenset[Producer]:
    """What can end a wait on a turn's checkpoint: the checkpoint or turn inspection, live or sent."""
    found = _from_records(
        _with_successors(records, _checkpoint_records(records, invocation)), outbox
    )
    sent = any(
        isinstance(request, SnapshotAndRetain | SnapshotAndRetainRun | InspectTurn)
        and request.invocation == invocation
        for request in in_flight
    )
    return found | {Producer.PENDING_REQUEST} if sent else found


def draining_without_cancel(sessions: SessionsState, invocation: InvocationRef) -> bool:
    """Whether an accepted interruption of *invocation* waits only on its checkpoint.

    The claim is draining and the turn already ended, so cancellation is done and the
    one thing left to wait for is the retained checkpoint (then the refund).
    """
    claim = next((row for row in sessions.interrupts if row.invocation == invocation), None)
    turn = next((row for row in sessions.invocations if row.invocation == invocation), None)
    observation = None if turn is None else turn.observation
    return (
        claim is not None
        and claim.phase == "draining"
        and observation is not None
        and observation.terminal
        and observation.status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
    )


def awaiting_refund(sessions: SessionsState, invocation: InvocationRef) -> bool:
    """Whether an interruption of *invocation* retained its checkpoint and waits to refund.

    The refund is applied by a signal inside the step that retained the checkpoint, so a claim
    still in this phase when the step is over has nothing left that could refund it.
    """
    return any(
        row.invocation == invocation and row.phase == "checkpointed" for row in sessions.interrupts
    )


def _checkpoint_waiters(sessions: SessionsState) -> tuple[InvocationRef, ...]:
    """Every invocation with a wait that its checkpoint (or turn inspection) or refund can end."""
    return tuple(
        dict.fromkeys(
            turn.invocation
            for turn in sessions.invocations
            if turn.pending_suspension is not None
            or draining_without_cancel(sessions, turn.invocation)
            or awaiting_refund(sessions, turn.invocation)
        )
    )


def stalled_checkpoint_waits(
    sessions: SessionsState, records: Sequence[RequestRecord], in_flight: Sequence[Request] = ()
) -> tuple[InvocationRef, ...]:
    """The invocations whose checkpoint wait nothing live or in flight can end.

    Two waits hang on a checkpoint: a suspension core promised to publish, and an accepted
    interruption that must retain the workspace before it refunds. *in_flight* holds the
    requests a step has produced but not yet recorded as records. An invocation named here
    would stall the run, so the step ends the waits instead (see ``CheckpointStalled``):
    this is the resolver every checkpoint wait is guaranteed.
    """
    return tuple(
        invocation
        for invocation in _checkpoint_waiters(sessions)
        if awaiting_refund(sessions, invocation)
        or not _checkpoint_producers(records, in_flight, frozenset(), invocation)
    )


def undispatchable_requests(state: CoreState) -> tuple[RequestId, ...]:
    """The prepared requests that can never be dispatched, so the step cancels them unsent.

    Two facts make a prepared request undispatchable for good. A dependency it declared
    failed (a dependent is cancelled when its dependency fails, but one prepared after that
    is never reached by that cancellation), or the episode that prepared it ended: the
    attempt closed or a stop retired its scope while the request waited for a slot. Dispatch
    authorization refuses both forever. The acceptance of the request's own decision is
    established when it is registered and is not re-checked here.
    """
    return tuple(
        row.request_id
        for row in state.records.records
        if row.phase == RequestPhase.PREPARED
        and (dependency_failed(row.request, state.records) or episode_ended(state, row.request))
    )


def _addresses(row: RequestRecord, record: RequestRecord) -> bool:
    """Whether *row* is a live request that can still end *record*: a cancel or inspection of it."""
    return (
        record_waits(row.phase)
        and row.request_id != record.request_id
        and getattr(row.request, "target", None) == record.request_id
    )


def _blocked_records(core: CoreState) -> tuple[RequestRecord, ...]:
    """The blocked records a closing run holds closure on, whether or not anything addresses them.

    A blocked record outside a close is the strategy's to handle (it heard ``RequestBlocked``).
    Once the run closes, closure waits on it, except on one that never owned anything
    (``blocked_unnamed``).
    """
    if core.run.status != RunStatus.CLOSING:
        return ()
    return tuple(
        row
        for row in core.records.records
        if row.phase == RequestPhase.BLOCKED and not blocked_unnamed(row)
    )


def _blocked_producers(
    records: Sequence[RequestRecord], outbox: frozenset[RequestId], record: RequestRecord
) -> frozenset[Producer]:
    return _from_records([row for row in records if _addresses(row, record)], outbox)


def stalled_blocked_records(core: CoreState) -> tuple[RequestId, ...]:
    """The blocked records of a closing run that no live cancellation or inspection can end.

    Each is a wait nothing produces: its bound passed and the close would sit on it forever, so
    the step ends it (``BlockedRecordStalled``).
    """
    return tuple(
        record.request_id
        for record in _blocked_records(core)
        if not _blocked_producers(core.records.records, frozenset(), record)
    )


def _released_job_edges(core: CoreState) -> list[str]:
    """A closing attempt's dependencies on jobs that are already released.

    A job that is still live is named by its own wait. A closing attempt that still lists a job
    as unreleased after the job ended, with its release recorded, waits for an event that was
    already due: the attempt can never reach terminal, and its settlement never completes.
    """
    released = {
        job.resource_id
        for job in (*core.evaluation.jobs, *core.evaluation.registered_jobs)
        if job.resource_id is not None and job.terminal and job.released
    }
    return [
        edge.identity.root
        for attempt in core.attempts.attempts
        if attempt.phase in (AttemptPhase.CLOSING, AttemptPhase.BLOCKED)
        for edge in attempt.release_dependencies
        if edge.kind == "job" and edge.identity in released
    ]


def subjects(core: CoreState) -> tuple[WaitSubject, ...]:
    """Every entity in *core* that waits on a request, an event or a timer to move on."""
    found: list[tuple[WaitKind, str]] = [
        *(
            (WaitKind.SESSION, session.spec.session_id.root)
            for session in core.sessions.sessions
            if session_waits(session.phase)
        ),
        *(
            (WaitKind.TURN_ACQUISITION, row.invocation.invocation_id.root)
            for row in _acquiring(core.sessions)
        ),
        *(
            (WaitKind.RECOVERY_CHECK, check.target.root)
            for check in core.records.recovery.checks
            if recovery_waits(core.records.recovery.phase) and check.resolution == "pending"
        ),
        *(
            (WaitKind.OWNED_JOB, job.resource_id.root)
            for job in core.evaluation.jobs
            if not job.terminal
        ),
        *(
            (WaitKind.OWNED_JOB, job.request_id.root)
            for job in core.evaluation.registered_jobs
            if not job.terminal
        ),
        *((WaitKind.RELEASE_DEPENDENCY, identity) for identity in _released_job_edges(core)),
    ]
    for turn in core.sessions.invocations:
        identity = turn.invocation.invocation_id.root
        if turn.pending_suspension is not None:
            found.append((WaitKind.YIELD_PUBLICATION, identity))
        if draining_without_cancel(core.sessions, turn.invocation):
            found.append((WaitKind.INTERRUPT, identity))
        if awaiting_refund(core.sessions, turn.invocation):
            found.append((WaitKind.REFUND, identity))
    found += [
        *(
            (WaitKind.DISPATCH, row.request_id.root)
            for row in core.records.records
            if row.phase == RequestPhase.PREPARED
        ),
        *(
            (WaitKind.RETRY, row.request_id.root)
            for row in core.records.records
            if row.retry_wait_until is not None
        ),
        *((WaitKind.BLOCKED_RECORD, row.request_id.root) for row in _blocked_records(core)),
    ]
    return tuple(WaitSubject(kind=kind, identity=identity) for kind, identity in found)


def _invocation(core: CoreState, identity: str) -> InvocationRef:
    return next(
        row.invocation
        for row in core.sessions.invocations
        if row.invocation.invocation_id.root == identity
    )


def _dispatch_producers(core: CoreState, identity: str) -> frozenset[Producer]:
    """A prepared request is dispatched by the shell, unless core can never allow it."""
    if identity in {row.root for row in undispatchable_requests(core)}:
        return frozenset()
    return frozenset({Producer.PENDING_REQUEST})


def _blocked_record_producers(
    core: CoreState, identity: str, outbox: frozenset[RequestId]
) -> frozenset[Producer]:
    record = next(row for row in core.records.records if row.request_id.root == identity)
    return _blocked_producers(core.records.records, outbox, record)


def _recovery_check_producers(
    core: CoreState, identity: str, outbox: frozenset[RequestId]
) -> frozenset[Producer]:
    target = next(row.target for row in core.records.recovery.checks if row.target.root == identity)
    return _recovery_producers(core, outbox, target)


type _Derivation = Callable[[CoreState, str, frozenset[RequestId]], frozenset[Producer]]

#: How the producers of each kind of wait are derived, from the waiting entity's identity.
#: A release recorded after the attempt stopped listening is not re-announced, and a refund is
#: a signal of the step that retained the checkpoint, so nothing can produce either.
_DERIVATIONS: Mapping[WaitKind, _Derivation] = MappingProxyType(
    {
        WaitKind.SESSION: lambda core, identity, outbox: _session_producers(
            core.records.records, outbox, SessionId(root=identity)
        ),
        WaitKind.TURN_ACQUISITION: lambda core, identity, outbox: _acquisition_producers(
            core.sessions, core.records.records, (), outbox, _invocation(core, identity)
        ),
        WaitKind.YIELD_PUBLICATION: lambda core, identity, outbox: _checkpoint_producers(
            core.records.records, (), outbox, _invocation(core, identity)
        ),
        WaitKind.INTERRUPT: lambda core, identity, outbox: _checkpoint_producers(
            core.records.records, (), outbox, _invocation(core, identity)
        ),
        WaitKind.RECOVERY_CHECK: _recovery_check_producers,
        WaitKind.OWNED_JOB: lambda core, identity, outbox: _job_producers(core, outbox, identity),
        WaitKind.RELEASE_DEPENDENCY: lambda _core, _identity, _outbox: frozenset(),
        WaitKind.REFUND: lambda _core, _identity, _outbox: frozenset(),
        WaitKind.DISPATCH: lambda core, identity, _outbox: _dispatch_producers(core, identity),
        WaitKind.RETRY: lambda _core, _identity, _outbox: frozenset({Producer.TIMER}),
        WaitKind.BLOCKED_RECORD: _blocked_record_producers,
    }
)


def producers(
    core: CoreState, subject: WaitSubject, outbox: Sequence[CoreEvent] = ()
) -> frozenset[Producer]:
    """Everything live that can move *subject* on: the one derivation every wait reads.

    ``waits`` reports each subject with the producer that ends it, and each resolver the step
    runs (``stalled_acquisitions``, ``stalled_checkpoint_waits``, ``stalled_blocked_records``,
    ``undispatchable_requests``) ends exactly the subjects of its kind this returns empty for.
    *outbox* holds the events committed but not yet applied (the shell's pending inputs).
    """
    return _DERIVATIONS[subject.kind](core, subject.identity, _outbox_requests(outbox))


def waits(core: CoreState, outbox: Sequence[CoreEvent] = ()) -> tuple[OpenWait, ...]:
    """Every wait in *core*, each with the producer that will end it (``producers``).

    *outbox* holds the events committed but not yet applied (the shell's pending inputs).
    """
    return tuple(
        OpenWait(
            waiter=subject.kind,
            subject=subject.identity,
            producer=_reported(producers(core, subject, outbox)),
        )
        for subject in subjects(core)
    )


def orphan_waits(core: CoreState, outbox: Sequence[CoreEvent] = ()) -> tuple[OpenWait, ...]:
    """The waits nothing will end. A terminal run waits on nothing."""
    if core.run.status == RunStatus.TERMINAL:
        return ()
    return tuple(wait for wait in waits(core, outbox) if wait.producer is None)


__all__ = [
    "RESOLUTIONS",
    "OpenWait",
    "Outcome",
    "Producer",
    "Resolution",
    "WaitKind",
    "WaitSubject",
    "WaitingPhase",
    "awaiting_refund",
    "draining_without_cancel",
    "orphan_waits",
    "phase_waits",
    "producers",
    "stalled_acquisitions",
    "stalled_blocked_records",
    "stalled_checkpoint_waits",
    "subjects",
    "undispatchable_requests",
    "waits",
]
