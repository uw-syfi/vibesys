"""One cleanup fence for irreversible run closure and terminal publication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._proofs import Proven, released_owner
from .types.attempts import AttemptPhase
from .types.common import LifecycleClass

if TYPE_CHECKING:
    from .types.evaluation import OwnedJob, RegisteredOwnedJob
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
                not _job_release_confirmed(job, state.intents.intents)
                for job in (*state.evaluation.jobs, *state.evaluation.registered_jobs)
            ),
            any(
                intent.phase != IntentPhase.COMPLETED
                or (
                    intent.lifecycle in (LifecycleClass.OWNED_JOB, LifecycleClass.SESSION_TURN)
                    and (not _intent_release_confirmed(intent, state.intents.intents))
                )
                for intent in state.intents.intents
            ),
            bool(state.settlement.pending),
            any(claim.phase != "completed" for claim in state.sessions.interrupts),
            _unreleased_children(state),
            any(group.phase == "acquiring" for group in state.sessions.acquisition_groups),
            any(
                not _child_release_confirmed(child, state.intents.intents)
                for child in state.intents.children
            ),
            any(
                invocation.phase
                not in (SessionPhase.TERMINAL, SessionPhase.CHECKPOINTED, SessionPhase.IDLE)
                for invocation in state.sessions.invocations
            ),
        )
    )


def _job_release_confirmed(job: OwnedJob | RegisteredOwnedJob, sources: tuple[Intent, ...]) -> bool:
    return isinstance(released_owner(job, sources), Proven)


def _intent_release_confirmed(intent: Intent, sources: tuple[Intent, ...]) -> bool:
    return isinstance(released_owner(intent, sources), Proven)


def _child_release_confirmed(child: ChildLease, sources: tuple[Intent, ...]) -> bool:
    return isinstance(released_owner(child, sources), Proven)


def _unreleased_children(state: CoreState) -> bool:
    jobs = (*state.evaluation.jobs, *state.evaluation.registered_jobs)
    released = {
        (job.resource_id, job.scope)
        for job in jobs
        if _job_release_confirmed(job, state.intents.intents)
    }
    released.update(
        (child.resource_id, child.scope)
        for child in state.intents.children
        if _child_release_confirmed(child, state.intents.intents)
    )
    observations = tuple(
        intent.observation for intent in state.intents.intents if intent.observation is not None
    )
    observations += tuple(job.observation for job in jobs if job.observation is not None)
    observations += tuple(
        child.observation for child in state.intents.children if child.observation is not None
    )
    observations += tuple(
        mark.observation
        for child in state.intents.children
        for mark in child.observation_watermarks
    )
    released.update(
        (intent.observation.resource_id, intent.observation.scope)
        for intent in state.intents.intents
        if intent.lifecycle in (LifecycleClass.OWNED_JOB, LifecycleClass.SESSION_TURN)
        and intent.observation is not None
        and _intent_release_confirmed(intent, state.intents.intents)
    )
    children = tuple((child, job.scope) for job in jobs for child in job.children)
    children += tuple(
        (child, observation.scope) for observation in observations for child in observation.children
    )
    return any(child not in released for child in children)
