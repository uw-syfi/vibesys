"""Every wait names what will end it: a pure check over core state, no state change.

Core waits in several places: a record in flight, a session acquiring, executing or
closing, an owned job. Each one is ended by something that already exists:
a request the shell still holds, an event committed but not yet applied (the outbox), or a
timer. A wait with none of these is an orphan: nothing will ever end it, so the run would sit
until its deadline. ``orphan_waits`` reports it at the step that created it.

Every waiter records what it waits on when it starts waiting (``WaitOn``), and nothing is
inferred from the ledger:

- an owned job names its poll timer or the poll in flight;
- a session names its ensure, turn, inspection or close;
- an acquiring turn names its session;
- a yield or a draining interruption names the checkpoint request or turn inspection it needs;
- a blocked record names the inspections, cancels and blocks sent about it
  (``RequestRecord.addressed_by``);
- a prepared request waits on its own dispatch, and a retryably failed one on its retry time.

A waiter may name another waiting entity only as ``MAY_WAIT_ON`` allows (I1: the graph is
acyclic), and the entity carries it while its own recorded wait does. A named request whose
answer the shell committed but has not applied (a turn's reply rides its session's owner
event, a step after the turn's own end) still counts. A named request that ends without
moving its waiter wakes it (``lapsed_job_waits``, ``lapsed_session_waits``), and
``unmet_waits`` refuses a step that records a wait on a request not open, or leaves a job,
session, turn, yield or interruption waiting without one. ``waits`` and every resolver below
read the same recorded waits, so a wait and its resolver cannot disagree about whether
something will end it.

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
from .types.attempts import AttemptPhase
from .types.common import (
    InvocationRef,
    ObservationStatus,
    RunStatus,
    SessionId,
    Value,
)
from .types.evaluation import ContinuationPhase, JobWaitLapsed, OwnedJob, RegisteredOwnedJob
from .types.records import (
    BlockRequest,
    CancelOwnedResource,
    InspectRequest,
    RequestPhase,
    phase_open,
    reply_owed,
)
from .types.sessions import (
    DispatchTurn,
    ResumeSessionTurn,
    SessionPhase,
    SessionWaitLapsed,
    session_waits,
    turn_acquires,
)
from .types.waits import WaitKind, WaitOn, WaitSubject

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

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
            "other requests still open. Once none is left, an unknown session cancels its turn "
            "as lost or closes its known lease, and a closing session inspects its unanswered "
            "turn once and sends its close once more. The run halts when the turn is still not "
            "ended after that inspection (a turn that may be running is never released), when "
            "the second close ends unanswered too, or for an ambiguous ensure whose lease core "
            "does not know",
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


type WaitingPhase = RequestPhase | SessionPhase | ContinuationPhase


def phase_waits(phase: WaitingPhase) -> bool:
    """Whether an entity in *phase* waits on a request, event or timer to end the phase."""
    if isinstance(phase, RequestPhase):
        return phase_open(phase)
    if isinstance(phase, SessionPhase):
        return session_waits(phase)
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
    sent = _taken_on(in_flight)
    return tuple(
        row.invocation
        for row in _acquiring(sessions)
        if row.wait is None
        or not _live(records, _session_waits_of(sessions), row.wait, frozenset(), sent)
    )


#: Which kinds of waiter may name which kinds of waiting entity (I1 of #2428). The graph is
#: acyclic, so following named entities always ends at requests and timers, and a waiter only
#: names an entity whose progress carries it: an acquiring turn names its session.
MAY_WAIT_ON: Mapping[WaitKind, frozenset[WaitKind]] = MappingProxyType(
    {
        WaitKind.TURN_ACQUISITION: frozenset({WaitKind.SESSION}),
        **{kind: frozenset() for kind in WaitKind if kind is not WaitKind.TURN_ACQUISITION},
    }
)


#: The kinds a queued answer cannot move, however they are reached. Core forwards a job's
#: answer to the job in the step that records it, so an answer still queued in the shell
#: belongs to a request core holds open anyway.
_MOVED_ONLY_BY_OPEN_REQUESTS = frozenset({WaitKind.OWNED_JOB})


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
    return _live(core.records, dict(recorded_waits(core)), wait, queued, frozenset())


def _live(
    records: RecordsState,
    named: Mapping[WaitSubject, WaitOn],
    wait: WaitOn,
    queued: frozenset[RequestId],
    sent: frozenset[RequestId],
) -> frozenset[Producer]:
    """The producers of *wait*: its open or just-sent requests, queued answers, entities, timer.

    *named* holds the recorded wait of each entity a wait may name. A named entity carries
    the waiter while its own recorded wait has a producer; one without a recorded wait
    carries nothing. The chain ends because ``MAY_WAIT_ON`` is acyclic.
    """
    found: set[Producer] = set()
    if any(_open(records, request) or request in sent for request in wait.requests):
        found.add(Producer.PENDING_REQUEST)
    if any(request in queued for request in wait.requests):
        found.add(Producer.OUTBOX_EVENT)
    for subject in wait.subjects:
        if (entity := named.get(subject)) is not None:
            heard = frozenset() if subject.kind in _MOVED_ONLY_BY_OPEN_REQUESTS else queued
            found |= _live(records, named, entity, heard, sent)
    if wait.timer is not None:
        found.add(Producer.TIMER)
    return frozenset(found)


def _session_waits_of(sessions: SessionsState) -> dict[WaitSubject, WaitOn]:
    """The recorded wait of each session, by the subject that names it."""
    return {
        WaitSubject(kind=WaitKind.SESSION, identity=row.spec.session_id.root): row.wait
        for row in sessions.sessions
        if row.wait is not None
    }


def _open(
    records: RecordsState, request_id: RequestId, seen: frozenset[RequestId] = frozenset()
) -> bool:
    """Whether something of this epoch can still end the request.

    A request still waiting on its executor can, unless a dead host dispatched it: no call of
    this epoch is behind it. It is taken on again by a command of this epoch about it, named
    in ``addressed_by``: one still open, or one this epoch's executor already answered (an
    inspection that found it still running, so its executor reports on it now). A
    reconciliation command that ended still carries the commands sent about it (an
    inspection of a cancel), and a wait on the command follows them to the record it
    addresses. A canonical record that ended has nothing left to wait for.
    """
    record = records.record(request_id)
    if record is None or request_id in seen:
        return False
    waiting = phase_open(record.phase)
    if waiting and not _dispatched_by_dead_host(records, record):
        return True
    if not waiting and not _reconciliation_command(record):
        return False
    return any(
        (waiting and _answered_in_this_epoch(records, command))
        or _open(records, command, seen | {request_id})
        for command in record.addressed_by
    )


def _answered_in_this_epoch(records: RecordsState, request_id: RequestId) -> bool:
    command = records.record(request_id)
    return (
        command is not None
        and not phase_open(command.phase)
        and command.dispatched_in == records.epoch
    )


def _taken_on(in_flight: Sequence[Request]) -> frozenset[RequestId]:
    """The requests a step's unrecorded requests carry: each one, and what a command addresses.

    A command the step sends about a request a dead host dispatched (recovery's inspection
    of it) takes it on in this epoch before ``addressed_by`` records the command.
    """
    sent = {request.request_id for request in in_flight if request.request_id}
    sent.update(
        request.target
        for request in in_flight
        if isinstance(request, InspectRequest | CancelOwnedResource | BlockRequest)
    )
    return frozenset(sent)


def _dispatched_by_dead_host(records: RecordsState, record: RequestRecord) -> bool:
    """Whether a host of an earlier epoch made the request's latest dispatch."""
    return (
        record.phase != RequestPhase.PREPARED
        and record.dispatched_in is not None
        and record.dispatched_in < records.epoch
    )


def _reconciliation_command(record: RequestRecord) -> bool:
    return isinstance(record.request, InspectRequest | CancelOwnedResource | BlockRequest)


def _request_open(core: CoreState, request_id: RequestId) -> bool:
    """Whether the ledger holds *request_id* still waiting on its executor, in any epoch."""
    record = core.records.record(request_id)
    return record is not None and phase_open(record.phase)


def owned_job_subject(job: OwnedJob | RegisteredOwnedJob) -> WaitSubject:
    """The subject that names an owned job: its resource, or its registered request."""
    identity = job.request_id if isinstance(job, RegisteredOwnedJob) else job.resource_id
    return WaitSubject(kind=WaitKind.OWNED_JOB, identity=identity.root)


def _job_waits(core: CoreState) -> tuple[tuple[WaitSubject, RequestId, WaitOn], ...]:
    """Every live job's recorded wait, with the subject that names the job and its own request."""
    jobs = (
        (owned_job_subject(job), job.submission_id, job.wait)
        for job in core.evaluation.jobs
        if job.wait is not None
    )
    registered = (
        (owned_job_subject(job), job.request_id, job.wait)
        for job in core.evaluation.registered_jobs
        if job.wait is not None
    )
    return (*jobs, *registered)


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


def _checkpoint_waits(core: CoreState) -> tuple[tuple[WaitSubject, WaitOn], ...]:
    """Every yield's and draining interruption's recorded wait, by the subject naming it."""
    yields = (
        (
            WaitSubject(
                kind=WaitKind.YIELD_PUBLICATION, identity=row.invocation.invocation_id.root
            ),
            row.wait,
        )
        for row in core.sessions.invocations
        if row.pending_suspension is not None and row.wait is not None
    )
    claims = (
        (WaitSubject(kind=WaitKind.INTERRUPT, identity=row.invocation.invocation_id.root), row.wait)
        for row in core.sessions.interrupts
        if row.wait is not None
    )
    return (*yields, *claims)


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
        *_checkpoint_waits(core),
    )


def lapsed_job_waits(core: CoreState) -> tuple[JobWaitLapsed, ...]:
    """One wake per live job whose wait names a request that is over.

    A job's own transition takes an answer of its poll and waits again, so a job still naming
    an ended request is one the answer did not reach: the poll blocked at its bound, or the
    job refused what it said. The wake makes the job wait again (``JobWaitLapsed``). An ended
    job whose evidence collection is over (``OwnedJob.collecting``) is woken too, so the
    comparisons its run serves are re-checked.
    """
    if core.run.status == RunStatus.TERMINAL:
        return ()
    polls = tuple(
        JobWaitLapsed(job=owner, request_id=request_id)
        for _, owner, wait in _job_waits(core)
        for request_id in wait.requests
        if _request_ended(core, request_id)
    )
    # An ended job collecting its evidence hears the collection end the same way, when no
    # answer of it reached the job (it blocked at its bound).
    collections = tuple(
        JobWaitLapsed(job=job.submission_id, request_id=job.collecting)
        for job in core.evaluation.jobs
        if job.collecting is not None and _request_ended(core, job.collecting)
    )
    return (*polls, *collections)


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
        if _request_ended(core, request_id) and not _reply_in_transit(core, request_id)
    )


def _reply_in_transit(core: CoreState, request_id: RequestId) -> bool:
    """Whether *request_id* is a turn whose reply its session's owner event still carries.

    A plain turn's success carries its reply on the owner event the shell commits with the
    observation and applies in the next step (``reply_owed``), so the session that names the
    turn has not heard it yet and must keep naming it.
    """
    record = core.records.record(request_id)
    return (
        record is not None
        and isinstance(record.request, DispatchTurn | ResumeSessionTurn)
        and record.observation is not None
        and reply_owed(record.observation, record)
    )


def _request_ended(core: CoreState, request_id: RequestId) -> bool:
    """Whether the ledger holds *request_id* and core applied its end.

    A request the step has prepared but not yet recorded is not ended.
    """
    record = core.records.record(request_id)
    return record is not None and not phase_open(record.phase)


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
    named.extend(_unnamed_checkpoint_waits(before, after))
    return tuple(named)


def _unnamed_checkpoint_waits(before: CoreState, after: CoreState) -> list[str]:
    """A yield or a draining interruption the step left waiting on its checkpoint unnamed.

    The stall ends such a wait in the step that finds it, so one still there at the end is a
    transition that forgot to name the request.
    """
    turns = before.sessions.invocations
    claims = before.sessions.interrupts
    named = [
        f"turn {turn.invocation.invocation_id.root} yields without a wait"
        for turn in after.sessions.invocations
        if turn.pending_suspension is not None and turn.wait is None and turn not in turns
    ]
    for claim in after.sessions.interrupts:
        turn = next(
            (row for row in after.sessions.invocations if row.invocation == claim.invocation), None
        )
        if (
            claim.wait is None
            and draining_without_cancel(after.sessions, claim.invocation)
            and (claim not in claims or turn not in turns)
        ):
            named.append(
                f"interruption of {claim.invocation.invocation_id.root} drains without a wait"
            )
    return named


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


def _yield_stalled(
    records: RecordsState, sessions: SessionsState, turn: Invocation, sent: frozenset[RequestId]
) -> bool:
    """Whether *turn*'s unpublished yield names nothing live: no request, or only ended ones."""
    return turn.pending_suspension is not None and (
        turn.wait is None
        or not _live(records, _session_waits_of(sessions), turn.wait, frozenset(), sent)
    )


def _interrupt_stalled(
    records: RecordsState, sessions: SessionsState, turn: Invocation, sent: frozenset[RequestId]
) -> bool:
    """Whether a draining interruption of *turn* waits on its checkpoint and names nothing live."""
    claim = next((row for row in sessions.interrupts if row.invocation == turn.invocation), None)
    return (
        claim is not None
        and draining_without_cancel(sessions, turn.invocation)
        and (
            claim.wait is None
            or not _live(records, _session_waits_of(sessions), claim.wait, frozenset(), sent)
        )
    )


def checkpoint_stalls(
    sessions: SessionsState,
    records: RecordsState,
    invocation: InvocationRef,
    sent: frozenset[RequestId] = frozenset(),
) -> tuple[bool, bool]:
    """Whether the yield of *invocation*, and its interruption, have nothing left to end them.

    The interruption also counts as stalled while it waits on a refund (``awaiting_refund``).
    *sent* names the requests the step sent and has not recorded yet.
    """
    turn = next((row for row in sessions.invocations if row.invocation == invocation), None)
    if turn is None:
        return False, False
    return (
        _yield_stalled(records, sessions, turn, sent),
        awaiting_refund(sessions, invocation) or _interrupt_stalled(records, sessions, turn, sent),
    )


def stalled_checkpoint_waits(
    sessions: SessionsState, records: RecordsState, in_flight: Sequence[Request] = ()
) -> tuple[InvocationRef, ...]:
    """The invocations whose checkpoint wait nothing live or in flight can end.

    Two waits hang on a checkpoint: a suspension core promised to publish, and an accepted
    interruption that must retain the workspace before it refunds. Each records the request
    that can end it (``Invocation.wait``, ``InterruptClaim.wait``): the checkpoint request the
    owning attempt confirmed (``InvocationCheckpointPending``), or the turn inspection that
    lets core request one. A wait that names nothing, because the checkpoint was declined, or
    only requests that ended, has nothing left. *in_flight* holds the requests a step has
    produced but not yet recorded. An invocation named here would stall the run, so the step
    ends the waits instead (see ``CheckpointStalled``): this is the resolver every checkpoint
    wait is guaranteed.
    """
    sent = _taken_on(in_flight)
    return tuple(
        invocation
        for invocation in _checkpoint_waiters(sessions)
        if any(checkpoint_stalls(sessions, records, invocation, sent))
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


def _blocked_waits(core: CoreState) -> tuple[tuple[WaitSubject, WaitOn], ...]:
    """A closing run's wait on each blocked record: the commands the record names.

    A record names each inspection, cancel and block of it as the command is registered
    (``RequestRecord.addressed_by``); one that names none waits on nothing.
    """
    return tuple(
        (
            WaitSubject(kind=WaitKind.BLOCKED_RECORD, identity=row.request_id.root),
            WaitOn(requests=row.addressed_by),
        )
        for row in _blocked_records(core)
        if row.addressed_by
    )


def stalled_blocked_records(core: CoreState) -> tuple[RequestId, ...]:
    """The blocked records of a closing run that no command they name can still end.

    Each is a wait nothing produces: its bound passed and the close would sit on it forever, so
    the step ends it (``BlockedRecordStalled``).
    """
    named = dict(_blocked_waits(core))
    return tuple(
        record.request_id
        for record in _blocked_records(core)
        if not _live_wait(
            core,
            named.get(WaitSubject(kind=WaitKind.BLOCKED_RECORD, identity=record.request_id.root)),
            frozenset(),
        )
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


def _ledger_waits(core: CoreState) -> tuple[tuple[WaitSubject, WaitOn], ...]:
    """The waits a request record states by its own fields.

    A prepared request waits on the shell to dispatch it (itself, while core can still allow
    it: ``undispatchable_requests``), and a retryably failed one on its retry time.
    """
    blocked = frozenset(undispatchable_requests(core))
    dispatch = (
        (
            WaitSubject(kind=WaitKind.DISPATCH, identity=row.request_id.root),
            WaitOn(requests=(row.request_id,)),
        )
        for row in core.records.records
        if row.phase == RequestPhase.PREPARED and row.request_id not in blocked
    )
    retry = (
        (WaitSubject(kind=WaitKind.RETRY, identity=row.request_id.root), WaitOn(timer=at))
        for row in core.records.records
        if (at := row.retry_wait_until) is not None
    )
    return (*dispatch, *retry)


def _live_wait(
    core: CoreState, wait: WaitOn | None, queued: frozenset[RequestId]
) -> frozenset[Producer]:
    """What can still end *wait*; a waiter that recorded none, nothing."""
    return frozenset() if wait is None else wait_producers(core, wait, queued)


def waits(core: CoreState, outbox: Sequence[CoreEvent] = ()) -> tuple[OpenWait, ...]:
    """Every wait in *core*, each with the producer that will end it, read from its record.

    Each waiter's wait is the one it recorded (``recorded_waits``), the commands a blocked
    record names, or what a request record states by its own fields (a prepared request's
    dispatch, a retry time). A waiter with none has no producer. *outbox* holds the events
    committed but not yet applied (the shell's pending inputs): an answer among them still
    moves the waits that name its request.
    """
    answers = _outbox_requests(outbox)
    table = {
        **dict(recorded_waits(core)),
        **dict(_blocked_waits(core)),
        **dict(_ledger_waits(core)),
    }
    return tuple(
        OpenWait(
            waiter=subject.kind,
            subject=subject.identity,
            producer=_reported(
                _live_wait(
                    core,
                    table.get(subject),
                    frozenset() if subject.kind in _MOVED_ONLY_BY_OPEN_REQUESTS else answers,
                )
            ),
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
