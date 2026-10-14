"""Every wait names what will end it: a pure check over core state, no state change.

Core waits in several places: a record in flight, a session acquiring, executing or
closing, a recovery check, an owned job. Each one is ended by something that already exists:
a request the shell still holds, an event committed but not yet applied (the outbox), or a
timer. A wait with none of these is an orphan: nothing will ever end it, so the run would sit
until its deadline. ``orphan_waits`` reports it at the step that created it.

An owned job, a session and an acquiring turn record what they wait on when they start
waiting (``WaitOn``: a job's poll timer or the poll in flight; the session's ensure, turn,
inspection or close; the turn's session), so their producers are read from that record and
nothing is inferred. A waiter may name another waiting entity only as ``MAY_WAIT_ON`` allows
(I1: the graph is acyclic), and the entity carries it while its own recorded wait does. A named request whose
answer the shell committed but has not applied (a turn's reply rides its session's owner
event, a step after the turn's own end) still counts. A named request that ends without
moving its waiter wakes it (``lapsed_job_waits``, ``lapsed_session_waits``), and
``unmet_waits`` refuses a step that records a wait on a request not open, or leaves a job or
session waiting without one. The other kinds are still derived, by ``producers``, until each
records its own wait (#2428). Either way the
waits report one answer, and every resolver below ends exactly the entities it finds nothing
for, so a wait and its resolver cannot disagree about whether something will end it.

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
    ContractValidationError,
    InvocationRef,
    ObservationStatus,
    RunStatus,
    SessionId,
    Value,
)
from .types.evaluation import ContinuationPhase, JobWaitLapsed
from .types.records import (
    InspectRequest,
    RecoveryPhase,
    RequestPhase,
)
from .types.sessions import (
    InspectTurn,
    SessionPhase,
    SessionWaitLapsed,
    SnapshotAndRetainRun,
    session_waits,
    turn_acquires,
)
from .types.waits import WaitKind, WaitOn, WaitSubject

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from .types.common import RequestId
    from .types.kernel import CoreEvent, CoreState
    from .types.records import RecordsState, Request, RequestRecord
    from .types.sessions import Invocation, SessionsState


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

    WAITS_AGAIN = "waits-again"
    """The ended request wakes the waiter, which waits again on a timer (``JobWaitLapsed``)."""

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
            reason="a request that ends without moving its session wakes it, and it waits on its "
            "other requests still open; once none is left (its last request blocked, or its "
            "cancel failed), it may hold a lease or a running turn that nothing releases",
        ),
        WaitKind.RECOVERY_CHECK: Resolution(
            outcome=Outcome.HALTS_THE_RUN,
            reason="a recovery check whose inspection was lost has no deadline of its own",
        ),
        WaitKind.OWNED_JOB: Resolution(
            outcome=Outcome.WAITS_AGAIN,
            reason="a poll that ends without moving its job (blocked at its bound, or its answer "
            "refused) wakes the job, which polls again after its backoff; a registered job whose "
            "operation ended without naming a job ends with what that request reported",
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


def _acquiring(sessions: SessionsState) -> tuple[Invocation, ...]:
    """The turns still acquiring: requested and charged or not, but with no result yet."""
    return tuple(row for row in sessions.invocations if turn_acquires(row))


def stalled_acquisitions(
    sessions: SessionsState, records: RecordsState, in_flight: Sequence[Request] = ()
) -> tuple[InvocationRef, ...]:
    """The acquiring turns that nothing can carry forward: each one's wait is an orphan.

    An acquiring turn waits on its session (``Invocation.wait``): the session's acquisition,
    or its reservation and dispatch once it holds the lease, carries the turn on. A turn whose
    session no longer waits on anything live, for example because its charge was never granted
    and the session sat idle, would stall the run, so the step ends it (``AcquisitionStalled``).
    *in_flight* holds the requests the step sent and has not recorded yet.
    """
    sent = frozenset(request.request_id for request in in_flight if request.request_id)
    return tuple(
        row.invocation
        for row in _acquiring(sessions)
        if row.wait is None or not _live(records, sessions, row.wait, frozenset(), sent)
    )


def _recovery_producers(core: CoreState, target: RequestId) -> frozenset[Producer]:
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
    return _children_producers(core, target)


def _children_producers(core: CoreState, target: RequestId) -> frozenset[Producer]:
    """What settles every child resource *target* left, when each is a job its ledger tracks.

    The job ledger is the other party that can settle such a child: once it observes the job
    ended, recovery hands the child to it and re-resolves the check. So each child waits on
    its job's own recorded wait, over both ledgers; one child no job answers for, or a job
    whose wait names nothing open, leaves the check an orphan. The check ends only once every
    child has settled, so it is the slowest child's producer that ends it, whatever order the
    children came in.
    """
    children = [child for child in core.records.children if target in child.source_requests]
    jobs = {
        (job.resource_id, job.scope): job.wait
        for job in (*core.evaluation.jobs, *core.evaluation.registered_jobs)
        if job.wait is not None and job.resource_id is not None
    }
    found: list[frozenset[Producer]] = [
        wait_producers(core, wait)
        if (wait := jobs.get((child.resource_id, child.scope))) is not None
        else frozenset()
        for child in children
    ]
    if not found or frozenset() in found:
        return frozenset()
    union = frozenset().union(*found)
    return frozenset({next(producer for producer in _SETTLES_LAST if producer in union)})


#: Which kinds of waiter may name which kinds of waiting entity (I1 of #2428). The graph is
#: acyclic, so following named entities always ends at requests and timers, and a waiter only
#: names an entity whose progress carries it: an acquiring turn names its session.
MAY_WAIT_ON: Mapping[WaitKind, frozenset[WaitKind]] = MappingProxyType(
    {
        kind: frozenset({WaitKind.SESSION}) if kind == WaitKind.TURN_ACQUISITION else frozenset()
        for kind in WaitKind
    }
)


def wait_producers(
    core: CoreState, wait: WaitOn, queued: frozenset[RequestId] = frozenset()
) -> frozenset[Producer]:
    """What can still end a recorded wait: an open request it names, or its timer.

    A request is open until core applies its end (its answer, or the block at its bound),
    so a request whose answer is still on its way counts as open. *queued* names the
    requests whose answer the shell committed and has not applied yet: a turn's reply
    travels on its session's owner event, committed with the turn's observation and applied
    in the next step, so a waiter named on the turn is still moved by it. The shell ticks the
    clock to every timer a wait names (``CoreView.next_observe_at``), so a timer always fires.
    """
    return _live(core.records, core.sessions, wait, queued, frozenset())


def _live(
    records: RecordsState,
    sessions: SessionsState,
    wait: WaitOn,
    queued: frozenset[RequestId],
    sent: frozenset[RequestId],
) -> frozenset[Producer]:
    """The producers of *wait*: its open or just-sent requests, queued answers, entities, timer.

    A named entity carries the waiter while its own recorded wait has a producer; the chain
    ends because ``MAY_WAIT_ON`` is acyclic.
    """
    found: set[Producer] = set()
    if any(_open(records, request) or request in sent for request in wait.requests):
        found.add(Producer.PENDING_REQUEST)
    if any(request in queued for request in wait.requests):
        found.add(Producer.OUTBOX_EVENT)
    for subject in wait.subjects:
        found |= _entity_producers(records, sessions, subject, queued, sent)
    if wait.timer is not None:
        found.add(Producer.TIMER)
    return frozenset(found)


def _entity_producers(
    records: RecordsState,
    sessions: SessionsState,
    subject: WaitSubject,
    queued: frozenset[RequestId],
    sent: frozenset[RequestId],
) -> frozenset[Producer]:
    """What carries a named session on: its own recorded wait's producers, or nothing."""
    if subject.kind != WaitKind.SESSION:
        raise ContractValidationError("wait.subjects", f"a wait cannot name a {subject.kind.value}")
    session = next(
        (row for row in sessions.sessions if row.spec.session_id.root == subject.identity), None
    )
    if session is None or session.wait is None:
        return frozenset()
    return _live(records, sessions, session.wait, queued, sent)


def _open(records: RecordsState, request_id: RequestId) -> bool:
    record = records.record(request_id)
    return record is not None and record_waits(record.phase)


def _request_open(core: CoreState, request_id: RequestId) -> bool:
    return _open(core.records, request_id)


def _job_waits(core: CoreState) -> tuple[tuple[WaitSubject, RequestId, WaitOn], ...]:
    """Every live job's recorded wait, with the subject that names the job and its own request."""
    found: list[tuple[WaitSubject, RequestId, WaitOn]] = []
    for job in core.evaluation.jobs:
        if job.wait is not None:
            subject = WaitSubject(kind=WaitKind.OWNED_JOB, identity=job.resource_id.root)
            found.append((subject, job.submission_id, job.wait))
    for registered in core.evaluation.registered_jobs:
        if registered.wait is not None:
            subject = WaitSubject(kind=WaitKind.OWNED_JOB, identity=registered.request_id.root)
            found.append((subject, registered.request_id, registered.wait))
    return tuple(found)


def _turn_waits(core: CoreState) -> tuple[tuple[WaitSubject, WaitOn], ...]:
    """Every acquiring turn's recorded wait, by the subject that names the turn."""
    return tuple(
        (
            WaitSubject(kind=WaitKind.TURN_ACQUISITION, identity=row.invocation.invocation_id.root),
            row.wait,
        )
        for row in _acquiring(core.sessions)
        if row.wait is not None
    )


def _session_waits(core: CoreState) -> tuple[tuple[WaitSubject, WaitOn], ...]:
    """Every waiting session's recorded wait, by the subject that names the session."""
    return tuple(
        (WaitSubject(kind=WaitKind.SESSION, identity=session.spec.session_id.root), session.wait)
        for session in core.sessions.sessions
        if session.wait is not None
    )


def recorded_waits(core: CoreState) -> tuple[tuple[WaitSubject, WaitOn], ...]:
    """Every wait an entity recorded when it began waiting, by the subject that names it."""
    return (
        *((subject, wait) for subject, _, wait in _job_waits(core)),
        *_session_waits(core),
        *_turn_waits(core),
    )


def lapsed_job_waits(core: CoreState) -> tuple[JobWaitLapsed, ...]:
    """One wake per live job whose wait names a request that is over.

    A job's own transition takes an answer of its poll and waits again, so a job still naming
    an ended request is one the answer did not reach: the poll blocked at its bound, or the
    job refused what it said. The wake makes the job wait again (``JobWaitLapsed``).
    """
    if core.run.status == RunStatus.TERMINAL:
        return ()
    return tuple(
        JobWaitLapsed(job=owner, request_id=request_id)
        for _, owner, wait in _job_waits(core)
        for request_id in wait.requests
        if _request_ended(core, request_id)
    )


def lapsed_session_waits(core: CoreState) -> tuple[SessionWaitLapsed, ...]:
    """One wake per waiting session whose wait names a request that is over.

    A session's own transition takes each answer of its requests, so a session still naming
    an ended request is one the answer did not move: the request blocked at its bound, or the
    session refused what it said. The wake makes the session wait again on what is left
    (``SessionWaitLapsed``).
    """
    if core.run.status == RunStatus.TERMINAL:
        return ()
    return tuple(
        SessionWaitLapsed(session_id=SessionId(root=subject.identity), request_id=request_id)
        for subject, wait in _session_waits(core)
        for request_id in wait.requests
        if _request_ended(core, request_id)
    )


def _request_ended(core: CoreState, request_id: RequestId) -> bool:
    """Whether the ledger holds *request_id* and core applied its end.

    A request the step has prepared but not yet recorded is not ended.
    """
    record = core.records.record(request_id)
    return record is not None and not record_waits(record.phase)


def unmet_waits(before: CoreState, after: CoreState) -> tuple[str, ...]:
    """Each wait the step recorded or changed that names a request not open (I2 of #2428).

    A wait names what will end it from the moment it is recorded, so every request it names
    exists and is open at the end of the step that recorded it, a request the step itself
    prepared included. A job live without a wait, or ended with one, and a session the step
    left in a waiting phase without a wait, are reported too: their validators hold only when
    a value is built or decoded, not when a transition copies one.
    """
    earlier = dict(recorded_waits(before))
    changed = [
        (subject, wait) for subject, wait in recorded_waits(after) if earlier.get(subject) != wait
    ]
    named = [
        f"{subject.kind.value} {subject.identity} waits on {request.root}"
        for subject, wait in changed
        for request in wait.requests
        if not _request_open(after, request)
    ]
    waiting = set(subjects(after))
    named.extend(
        f"{subject.kind.value} {subject.identity} waits on {entity.kind.value} "
        f"{entity.identity}, which it may not name or which is not waiting"
        for subject, wait in changed
        for entity in wait.subjects
        if entity.kind not in MAY_WAIT_ON[subject.kind] or entity not in waiting
    )
    named.extend(
        f"owned-job {job.resource_id.root} is live without a wait, or ended with one"
        for job in after.evaluation.jobs
        if job.terminal != (job.wait is None)
    )
    named.extend(
        f"owned-job {job.request_id.root} is live without a wait, or ended with one"
        for job in after.evaluation.registered_jobs
        if job.terminal != (job.wait is None)
    )
    unchanged = before.sessions.sessions
    named.extend(
        f"session {session.spec.session_id.root} is {session.phase.value} without a wait"
        for session in after.sessions.sessions
        if session_waits(session.phase) and session.wait is None and session not in unchanged
    )
    turns = before.sessions.invocations
    named.extend(
        f"turn {turn.invocation.invocation_id.root} is acquiring without a wait"
        for turn in after.sessions.invocations
        if turn_acquires(turn) and turn.wait is None and turn not in turns
    )
    return tuple(named)


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
            if session_waits(session.phase) or session.wait is not None
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
        *((subject.kind, subject.identity) for subject, _, _ in _job_waits(core)),
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


def _recovery_check_producers(core: CoreState, identity: str) -> frozenset[Producer]:
    target = next(row.target for row in core.records.recovery.checks if row.target.root == identity)
    return _recovery_producers(core, target)


def _session_wait_producers(
    core: CoreState, identity: str, queued: frozenset[RequestId]
) -> frozenset[Producer]:
    """What can end a waiting session's recorded wait; a waiting session without one, nothing."""
    session = next(row for row in core.sessions.sessions if row.spec.session_id.root == identity)
    return frozenset() if session.wait is None else wait_producers(core, session.wait, queued)


def _turn_wait_producers(
    core: CoreState, identity: str, queued: frozenset[RequestId]
) -> frozenset[Producer]:
    """What can carry an acquiring turn on: its recorded wait; one without a wait, nothing."""
    turn = next(
        row for row in _acquiring(core.sessions) if row.invocation.invocation_id.root == identity
    )
    return frozenset() if turn.wait is None else wait_producers(core, turn.wait, queued)


def _owned_job_producers(core: CoreState, identity: str) -> frozenset[Producer]:
    wait = next(
        wait
        for subject, wait in recorded_waits(core)
        if subject == WaitSubject(kind=WaitKind.OWNED_JOB, identity=identity)
    )
    return wait_producers(core, wait)


type _Derivation = Callable[[CoreState, str, frozenset[RequestId]], frozenset[Producer]]

#: How the producers of each kind of wait are derived, from the waiting entity's identity.
#: A release recorded after the attempt stopped listening is not re-announced, and a refund is
#: a signal of the step that retained the checkpoint, so nothing can produce either.
_DERIVATIONS: Mapping[WaitKind, _Derivation] = MappingProxyType(
    {
        WaitKind.SESSION: _session_wait_producers,
        WaitKind.TURN_ACQUISITION: _turn_wait_producers,
        WaitKind.YIELD_PUBLICATION: lambda core, identity, outbox: _checkpoint_producers(
            core.records.records, (), outbox, _invocation(core, identity)
        ),
        WaitKind.INTERRUPT: lambda core, identity, outbox: _checkpoint_producers(
            core.records.records, (), outbox, _invocation(core, identity)
        ),
        WaitKind.RECOVERY_CHECK: lambda core, identity, _outbox: _recovery_check_producers(
            core, identity
        ),
        WaitKind.OWNED_JOB: lambda core, identity, _outbox: _owned_job_producers(core, identity),
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
    "MAY_WAIT_ON",
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
    "lapsed_job_waits",
    "lapsed_session_waits",
    "orphan_waits",
    "phase_waits",
    "producers",
    "recorded_waits",
    "stalled_acquisitions",
    "stalled_blocked_records",
    "stalled_checkpoint_waits",
    "subjects",
    "undispatchable_requests",
    "unmet_waits",
    "wait_producers",
    "waits",
]
