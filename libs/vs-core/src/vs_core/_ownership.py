"""One cleanup fence for irreversible run closure and terminal publication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.attempts import AttemptPhase
from .types.common import LifecycleClass, ObservationStatus
from .types.evaluation import RegisteredOwnedJob

if TYPE_CHECKING:
    from .types.common import Observation
    from .types.evaluation import OwnedJob
    from .types.intents import ChildLease, Intent
from .types.intents import IntentPhase
from .types.sessions import SessionPhase

if TYPE_CHECKING:
    from .types.kernel import CoreState


def cleanup_pending(state: CoreState) -> bool:
    """Pending work and unresolved ownership cannot be hidden by admission drain."""
    return any(
        (
            bool(state.scheduling.queue or state.scheduling.slots),
            any(
                attempt.phase
                in (
                    AttemptPhase.QUEUED,
                    AttemptPhase.ACQUIRING,
                    AttemptPhase.ACTIVE,
                    AttemptPhase.CLOSING,
                    AttemptPhase.BLOCKED,
                )
                or attempt.release_dependencies
                for attempt in state.attempts.attempts
            ),
            any(session.phase != SessionPhase.TERMINAL for session in state.sessions.sessions),
            any(
                not _job_release_confirmed(job)
                for job in (*state.evaluation.jobs, *state.evaluation.registered_jobs)
            ),
            any(
                intent.phase != IntentPhase.COMPLETED
                or (
                    intent.lifecycle in (LifecycleClass.OWNED_JOB, LifecycleClass.SESSION_TURN)
                    and (not _intent_release_confirmed(intent))
                )
                for intent in state.intents.intents
            ),
            bool(state.settlement.pending),
            _unreleased_children(state),
            any(group.phase == "acquiring" for group in state.sessions.acquisition_groups),
            any(not _child_release_confirmed(child) for child in state.intents.children),
            any(
                invocation.phase
                not in (SessionPhase.TERMINAL, SessionPhase.CHECKPOINTED, SessionPhase.IDLE)
                for invocation in state.sessions.invocations
            ),
        )
    )


def _release_confirmed(observation: Observation | None) -> bool:
    """Release includes an authoritative child manifest, never an empty default."""
    return (
        observation is not None
        and observation.terminal
        and observation.released
        and observation.children_complete
        and observation.status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
    )


def _positive_nonownership(observation: Observation) -> bool:
    return not observation.accepted and observation.status in (
        ObservationStatus.REJECTED,
        ObservationStatus.FAILED,
        ObservationStatus.CANCELLED,
    )


def _job_release_confirmed(job: OwnedJob | RegisteredOwnedJob) -> bool:
    observation = job.observation
    if (
        observation is None
        or not job.terminal
        or not job.released
        or not _release_confirmed(observation)
    ):
        return False
    request_id = job.request_id if isinstance(job, RegisteredOwnedJob) else job.submission_id
    if observation.request_id != request_id or observation.scope != job.scope:
        return False
    if observation.resource_id != job.resource_id:
        return False
    if isinstance(job, RegisteredOwnedJob) and job.resource_id is None:
        return _positive_nonownership(observation)
    return True


def _intent_release_confirmed(intent: Intent) -> bool:
    observation = intent.observation
    if observation is None or not _release_confirmed(observation):
        return False
    if observation.request_id != intent.request_id or observation.scope != intent.request.scope:
        return False
    if intent.lifecycle == LifecycleClass.OWNED_JOB and observation.resource_id is None:
        return _positive_nonownership(observation)
    return True


def _child_release_confirmed(child: ChildLease) -> bool:
    observation = child.observation
    return (
        _release_confirmed(observation)
        and observation is not None
        and observation.resource_id == child.resource_id
        and observation.scope == child.scope
        and observation.request_id in child.source_requests
    )


def _unreleased_children(state: CoreState) -> bool:
    jobs = (*state.evaluation.jobs, *state.evaluation.registered_jobs)
    released = {(job.resource_id, job.scope) for job in jobs if _job_release_confirmed(job)}
    released.update(
        (child.resource_id, child.scope)
        for child in state.intents.children
        if _child_release_confirmed(child)
    )
    observations = tuple(
        intent.observation for intent in state.intents.intents if intent.observation is not None
    )
    observations += tuple(job.observation for job in jobs if job.observation is not None)
    observations += tuple(
        child.observation for child in state.intents.children if child.observation is not None
    )
    released.update(
        (intent.observation.resource_id, intent.observation.scope)
        for intent in state.intents.intents
        if intent.lifecycle in (LifecycleClass.OWNED_JOB, LifecycleClass.SESSION_TURN)
        and intent.observation is not None
        and _intent_release_confirmed(intent)
    )
    children = tuple((child, job.scope) for job in jobs for child in job.children)
    children += tuple(
        (child, observation.scope) for observation in observations for child in observation.children
    )
    return any(child not in released for child in children)
