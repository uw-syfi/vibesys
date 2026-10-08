"""Every wait names what will end it: a pure check over core state, no state change.

Core waits in several places: an intent in flight, a session acquiring, executing or
closing, a recovery check, an owned job. Each one is ended by something that already exists:
a request the shell still holds, an event committed but not yet applied (the outbox), or a
timer. A wait with none of these is an orphan: nothing will ever end it, so the run would sit
until its deadline. ``orphan_waits`` reports it at the step that created it.

A phase that waits on the strategy or the operator (idle, checkpointed, parked) is not
listed: the strategy's next proposal or the run deadline ends it. Each phase enum is matched
exhaustively below, so a new phase fails the type check and ``test_waits`` until someone
says whether it waits and what ends the wait.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from .types.common import LifecycleClass, RunStatus, Value
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
    ResumeSessionTurn,
    SessionPhase,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .types.common import RequestId
    from .types.intents import Intent
    from .types.kernel import CoreEvent, CoreState
    from .types.sessions import SessionView


class WaitKind(StrEnum):
    """What is waiting."""

    SESSION = "session"
    RECOVERY_CHECK = "recovery-check"
    OWNED_JOB = "owned-job"


class Producer(StrEnum):
    """What will end a wait."""

    PENDING_REQUEST = "pending-request"
    OUTBOX_EVENT = "outbox-event"
    TIMER = "timer"


class Wait(Value):
    """One waiting entity and the producer that will end its wait, or None for an orphan."""

    waiter: WaitKind
    subject: str
    producer: Producer | None = None


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
    request = intent.request
    identity = session.spec.session_id
    if isinstance(request, EnsureSession):
        return request.spec.session_id == identity
    if isinstance(request, CloseSession):
        return request.session_id == identity
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        return request.turn.session.session_id == identity
    if isinstance(request, ExecuteRegisteredOperation):
        return intent.lifecycle == LifecycleClass.SESSION_TURN
    return False


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
        *_recovery_waits(core),
        *_job_waits(core, pending),
    )


def orphan_waits(core: CoreState, outbox: Sequence[CoreEvent] = ()) -> tuple[Wait, ...]:
    """The waits nothing will end. A terminal run waits on nothing."""
    if core.run.status == RunStatus.TERMINAL:
        return ()
    return tuple(wait for wait in waits(core, outbox) if wait.producer is None)


__all__ = [
    "Producer",
    "Wait",
    "WaitKind",
    "WaitingPhase",
    "orphan_waits",
    "phase_waits",
    "waits",
]
