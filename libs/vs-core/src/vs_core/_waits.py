"""Every wait names what will end it: a pure check over core state, no state change.

Core waits in several places: an intent in flight, a session acquiring, executing or
closing, a recovery check, an owned job. Each one is ended by something that already exists:
a request the shell still holds, an event committed but not yet applied (the outbox), or a
timer. A wait with none of these is an orphan: nothing will ever end it, so the run would sit
until its deadline. ``orphan_waits`` reports it at the step that created it.

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
    Value,
)
from .types.evaluation import (
    ContinuationPhase,
    InspectOwnedJob,
    ObserveOwnedJob,
)
from .types.intents import (
    ExecuteRegisteredOperation,
    IntentPhase,
    RecoveryPhase,
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
    from collections.abc import Iterable, Mapping, Sequence

    from .types.common import RequestId, SessionId
    from .types.intents import Intent, Request
    from .types.kernel import CoreEvent, CoreState
    from .types.sessions import Invocation, SessionsState, SessionView


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


class Wait(Value):
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
            reason="after Limits.max_retries retries the request ends failed, not accepted",
        ),
        WaitKind.TURN_ACQUISITION: Resolution(
            outcome=Outcome.FAILS_WITH_REASON,
            reason="the turn ends cancelled with TurnResult.detail naming that nothing carried "
            "its acquisition forward; its decision completes cancelled",
        ),
        WaitKind.BLOCKED_RECORD: Resolution(
            outcome=Outcome.FAILS_WITH_REASON,
            reason="the record ends failed (diagnostic names the close), keeping the ownership "
            "facts it reported; the strategy already heard IntentBlocked",
        ),
        WaitKind.SESSION: Resolution(
            outcome=Outcome.HALTS_THE_RUN,
            reason="an intent blocked by its reconciliation bound is not routed to the session",
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


def intent_waits(phase: IntentPhase) -> bool:
    """Whether an intent in this phase waits on the shell to execute or inspect its request."""
    match phase:
        case IntentPhase.PREPARED | IntentPhase.DISPATCHED | IntentPhase.RECONCILING:
            return True
        case IntentPhase.COMPLETED | IntentPhase.BLOCKED:
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


type WaitingPhase = IntentPhase | SessionPhase | RecoveryPhase | ContinuationPhase


def phase_waits(phase: WaitingPhase) -> bool:
    """Whether an entity in *phase* waits on a request, event or timer to end the phase."""
    if isinstance(phase, IntentPhase):
        return intent_waits(phase)
    if isinstance(phase, SessionPhase):
        return session_waits(phase)
    if isinstance(phase, RecoveryPhase):
        return recovery_waits(phase)
    return continuation_waits(phase)


def _live(core: CoreState) -> tuple[Intent, ...]:
    return tuple(row for row in core.intents.intents if intent_waits(row.phase))


def _outbox_requests(outbox: Iterable[CoreEvent]) -> frozenset[RequestId]:
    """The requests whose observation is committed and waiting to be applied."""
    found: set[RequestId] = set()
    for event in outbox:
        observation = getattr(event, "observation", None)
        request_id = getattr(observation, "request_id", None)
        if request_id is not None:
            found.add(request_id)
    return frozenset(found)


def _about_session(intent: Intent, session: SessionView) -> bool:
    return _concerns(intent.request, session.spec.session_id, intent.lifecycle)


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


def _producer(related: Sequence[Intent], outbox: frozenset[RequestId]) -> Producer | None:
    if any(intent_waits(row.phase) for row in related):
        return Producer.PENDING_REQUEST
    if any(row.request_id in outbox for row in related):
        return Producer.OUTBOX_EVENT
    return None


def _job_producer(
    submission: RequestId | None,
    *,
    answered: bool,
    related: Sequence[Intent],
    outbox: frozenset[RequestId],
) -> Producer | None:
    """The producer of a job's next observation.

    A submission's own intent stays open until the job ends, so once the submission has
    been answered it no longer produces anything: only a poll in flight, a timer or an
    outbox event does.
    """
    live = [row for row in related if not (answered and row.request_id == submission)]
    if any(intent_waits(row.phase) for row in live):
        return Producer.PENDING_REQUEST
    if any(row.request_id in outbox for row in related):
        return Producer.OUTBOX_EVENT
    return None


def _session_waits(core: CoreState, outbox: frozenset[RequestId]) -> list[Wait]:
    found = []
    for session in core.sessions.sessions:
        if not session_waits(session.phase):
            continue
        related = [row for row in core.intents.intents if _about_session(row, session)]
        found.append(
            Wait(
                waiter=WaitKind.SESSION,
                subject=session.spec.session_id.root,
                producer=_producer(related, outbox),
            )
        )
    return found


def _acquiring(sessions: SessionsState) -> tuple[Invocation, ...]:
    """The turns still acquiring: requested and charged or not, but with no result yet."""
    return tuple(
        row
        for row in sessions.invocations
        if row.phase == SessionPhase.ACQUIRING and row.observation is None
    )


def _carried(
    sessions: SessionsState,
    intents: Sequence[Intent],
    in_flight: Sequence[Request],
    invocation: InvocationRef,
) -> bool:
    """Whether a live or in-flight request, or a turn interruption, can carry *invocation* on.

    A turn acquires through its session's lease request, its input reservation and its
    dispatch. A replacement turn also waits for the interruption of its predecessor, whose own
    waits are ended first (see ``stalled_checkpoint_waits``).
    """
    return (
        any(
            intent_waits(row.phase) and _concerns(row.request, invocation.session_id, row.lifecycle)
            for row in intents
        )
        or any(_concerns(request, invocation.session_id, None) for request in in_flight)
        or any(
            claim.invocation.session_id == invocation.session_id
            and claim.phase in ("pending", "draining", "checkpointed")
            for claim in sessions.interrupts
        )
    )


def stalled_acquisitions(
    sessions: SessionsState, intents: Sequence[Intent], in_flight: Sequence[Request] = ()
) -> tuple[InvocationRef, ...]:
    """The acquiring turns that nothing can carry forward.

    A turn acquiring without a producer, for example because its charge was never granted,
    would stall the run, so the step ends it (``AcquisitionStalled``).
    """
    return tuple(
        row.invocation
        for row in _acquiring(sessions)
        if not _carried(sessions, intents, in_flight, row.invocation)
    )


def _acquisition_waits(core: CoreState, outbox: frozenset[RequestId]) -> list[Wait]:
    """A turn acquiring waits for the session request that carries it forward."""
    found = []
    for row in _acquiring(core.sessions):
        related = [
            intent
            for intent in core.intents.intents
            if _concerns(intent.request, row.invocation.session_id, intent.lifecycle)
        ]
        carried = _carried(core.sessions, core.intents.intents, (), row.invocation)
        found.append(
            Wait(
                waiter=WaitKind.TURN_ACQUISITION,
                subject=row.invocation.invocation_id.root,
                producer=Producer.PENDING_REQUEST if carried else _producer(related, outbox),
            )
        )
    return found


def _recovery_waits(core: CoreState) -> list[Wait]:
    barrier = core.intents.recovery
    if not recovery_waits(barrier.phase):
        return []
    live = {row.request_id for row in _live(core)}
    return [
        Wait(
            waiter=WaitKind.RECOVERY_CHECK,
            subject=check.target.root,
            producer=Producer.PENDING_REQUEST
            if check.target in live or check.inspection in live
            else None,
        )
        for check in barrier.checks
        if check.resolution == "pending"
    ]


def _job_waits(core: CoreState, outbox: frozenset[RequestId]) -> list[Wait]:
    found = []
    for job in core.evaluation.jobs:
        if job.terminal:
            continue
        related = [
            row
            for row in core.intents.intents
            if row.request_id == job.submission_id or _observes(row, job.resource_id)
        ]
        producer = Producer.TIMER if job.pacing.next_at is not None else None
        found.append(
            Wait(
                waiter=WaitKind.OWNED_JOB,
                subject=job.resource_id.root,
                producer=producer
                or _job_producer(
                    job.submission_id,
                    answered=job.observation is not None,
                    related=related,
                    outbox=outbox,
                ),
            )
        )
    for registered in core.evaluation.registered_jobs:
        if registered.terminal:
            continue
        related = [
            row
            for row in core.intents.intents
            if row.request_id == registered.request_id
            or (registered.resource_id is not None and _observes(row, registered.resource_id))
        ]
        producer = Producer.TIMER if registered.pacing.next_at is not None else None
        found.append(
            Wait(
                waiter=WaitKind.OWNED_JOB,
                subject=registered.request_id.root,
                producer=producer
                or _job_producer(
                    registered.request_id,
                    answered=registered.observation is not None,
                    related=related,
                    outbox=outbox,
                ),
            )
        )
    return found


def _release_waits(core: CoreState) -> list[Wait]:
    """A closing attempt's dependency on a job that is already released.

    A job that is still live is named by its own wait above. A closing attempt that still lists
    a job as unreleased after the job ended, with its release recorded, waits for an event that
    was already due: the attempt can never reach terminal, and its settlement never completes.
    """
    released = {
        job.resource_id
        for job in (*core.evaluation.jobs, *core.evaluation.registered_jobs)
        if job.resource_id is not None and job.terminal and job.released
    }
    return [
        Wait(waiter=WaitKind.RELEASE_DEPENDENCY, subject=edge.identity.root, producer=None)
        for attempt in core.attempts.attempts
        if attempt.phase in (AttemptPhase.CLOSING, AttemptPhase.BLOCKED)
        for edge in attempt.release_dependencies
        if edge.kind == "job" and edge.identity in released
    ]


def _checkpoint_intents(intents: Iterable[Intent], invocation: InvocationRef) -> list[Intent]:
    """The intents that can still end a yield: its checkpoint, or the inspection of its turn.

    An inspection can end it because the turn's acceptance is what lets the checkpoint be
    requested. Both are about exactly this invocation.
    """
    return [
        row
        for row in intents
        if isinstance(row.request, SnapshotAndRetain | SnapshotAndRetainRun | InspectTurn)
        and row.request.invocation == invocation
    ]


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
    sessions: SessionsState, intents: Sequence[Intent], in_flight: Sequence[Request] = ()
) -> tuple[InvocationRef, ...]:
    """The invocations whose checkpoint wait nothing live or in flight can end.

    Two waits hang on a checkpoint: a suspension core promised to publish, and an accepted
    interruption that must retain the workspace before it refunds. *in_flight* holds the
    requests a step has produced but not yet recorded as intents. An invocation named here
    would stall the run, so the step ends the waits instead (see ``CheckpointStalled``):
    this is the resolver every checkpoint wait is guaranteed.
    """
    return tuple(
        invocation
        for invocation in _checkpoint_waiters(sessions)
        if awaiting_refund(sessions, invocation)
        or (
            not any(intent_waits(row.phase) for row in _checkpoint_intents(intents, invocation))
            and not any(
                isinstance(request, SnapshotAndRetain | SnapshotAndRetainRun | InspectTurn)
                and request.invocation == invocation
                for request in in_flight
            )
        )
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
        for row in state.intents.intents
        if row.phase == IntentPhase.PREPARED
        and (
            dependency_failed(row.request, state.run.receipts, state.intents)
            or episode_ended(state, row.request)
        )
    )


def _yield_waits(core: CoreState, outbox: frozenset[RequestId]) -> list[Wait]:
    """The waits that hang on a turn's checkpoint: its suspension and its interruption.

    Core publishes a suspension, and completes an interruption, when the checkpoint of the
    turn's workspace comes back, so each wait is produced by that checkpoint request. Core
    ends a wait nothing can produce (``stalled_checkpoint_waits``), so an orphan here means
    that resolver did not run.
    """
    found = []
    for invocation in core.sessions.invocations:
        kinds = []
        if invocation.pending_suspension is not None:
            kinds.append(WaitKind.YIELD_PUBLICATION)
        if draining_without_cancel(core.sessions, invocation.invocation):
            kinds.append(WaitKind.INTERRUPT)
        related = _checkpoint_intents(core.intents.intents, invocation.invocation)
        found.extend(
            Wait(
                waiter=kind,
                subject=invocation.invocation.invocation_id.root,
                producer=_producer(related, outbox),
            )
            for kind in kinds
        )
        if awaiting_refund(core.sessions, invocation.invocation):
            # The refund is a signal of the step that retained the checkpoint: no request,
            # event or timer outlives that step, so a claim still waiting has no producer.
            found.append(
                Wait(
                    waiter=WaitKind.REFUND,
                    subject=invocation.invocation.invocation_id.root,
                    producer=None,
                )
            )
    return found


def _dispatch_waits(core: CoreState) -> list[Wait]:
    """A prepared request waits for the shell to dispatch it, which core must be able to allow."""
    undispatchable = set(undispatchable_requests(core))
    return [
        Wait(
            waiter=WaitKind.DISPATCH,
            subject=row.request_id.root,
            producer=None if row.request_id in undispatchable else Producer.PENDING_REQUEST,
        )
        for row in core.intents.intents
        if row.phase == IntentPhase.PREPARED
    ]


def _addresses(row: Intent, record: Intent) -> bool:
    """Whether *row* is a live request that can still end *record*: a cancel or inspection of it."""
    return (
        intent_waits(row.phase)
        and row.request_id != record.request_id
        and getattr(row.request, "target", None) == record.request_id
    )


def _blocked_records(core: CoreState) -> tuple[Intent, ...]:
    """The blocked intents a closing run holds closure on, whether or not anything addresses them.

    A blocked record outside a close is the strategy's to handle (it heard ``IntentBlocked``).
    Once the run closes, closure waits on it, except on one that never owned anything
    (``blocked_unnamed``).
    """
    if core.run.status != RunStatus.CLOSING:
        return ()
    return tuple(
        row
        for row in core.intents.intents
        if row.phase == IntentPhase.BLOCKED and not blocked_unnamed(row)
    )


def stalled_blocked_records(core: CoreState) -> tuple[RequestId, ...]:
    """The blocked records of a closing run that no live cancellation or inspection can end.

    Each is a wait nothing produces: its bound passed and the close would sit on it forever, so
    the step ends it (``BlockedRecordStalled``).
    """
    return tuple(
        record.request_id
        for record in _blocked_records(core)
        if not any(_addresses(row, record) for row in core.intents.intents)
    )


def _blocked_record_waits(core: CoreState, outbox: frozenset[RequestId]) -> list[Wait]:
    found = []
    for record in _blocked_records(core):
        related = [row for row in core.intents.intents if _addresses(row, record)]
        found.append(
            Wait(
                waiter=WaitKind.BLOCKED_RECORD,
                subject=record.request_id.root,
                producer=_producer(related, outbox),
            )
        )
    return found


def _retry_waits(core: CoreState) -> list[Wait]:
    """A retryably failed request waits for its backoff timer, which core's clock tick fires."""
    return [
        Wait(waiter=WaitKind.RETRY, subject=row.request_id.root, producer=Producer.TIMER)
        for row in core.intents.intents
        if row.retry_wait_until is not None
    ]


def _observes(intent: Intent, resource: object) -> bool:
    request = intent.request
    return isinstance(request, ObserveOwnedJob | InspectOwnedJob) and (
        request.resource_id == resource
    )


def waits(core: CoreState, outbox: Sequence[CoreEvent] = ()) -> tuple[Wait, ...]:
    """Every wait in *core*, each with the producer that will end it.

    *outbox* holds the events committed but not yet applied (the shell's pending inputs).
    """
    pending = _outbox_requests(outbox)
    return (
        *_session_waits(core, pending),
        *_acquisition_waits(core, pending),
        *_recovery_waits(core),
        *_job_waits(core, pending),
        *_release_waits(core),
        *_yield_waits(core, pending),
        *_dispatch_waits(core),
        *_retry_waits(core),
        *_blocked_record_waits(core, pending),
    )


def orphan_waits(core: CoreState, outbox: Sequence[CoreEvent] = ()) -> tuple[Wait, ...]:
    """The waits nothing will end. A terminal run waits on nothing."""
    if core.run.status == RunStatus.TERMINAL:
        return ()
    return tuple(wait for wait in waits(core, outbox) if wait.producer is None)


__all__ = [
    "RESOLUTIONS",
    "Outcome",
    "Producer",
    "Resolution",
    "Wait",
    "WaitKind",
    "WaitingPhase",
    "awaiting_refund",
    "draining_without_cancel",
    "orphan_waits",
    "phase_waits",
    "stalled_acquisitions",
    "stalled_blocked_records",
    "stalled_checkpoint_waits",
    "undispatchable_requests",
    "waits",
]
