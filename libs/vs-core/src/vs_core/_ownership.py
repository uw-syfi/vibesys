"""One cleanup fence for irreversible run closure and terminal publication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.attempts import AttemptPhase
from .types.common import LifecycleClass
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
                    AttemptPhase.ACTIVE,
                    AttemptPhase.CLOSING,
                    AttemptPhase.BLOCKED,
                )
                or attempt.release_dependencies
                for attempt in state.attempts.attempts
            ),
            any(session.phase != SessionPhase.TERMINAL for session in state.sessions.sessions),
            any(
                not job.terminal or not job.released
                for job in (*state.evaluation.jobs, *state.evaluation.registered_jobs)
            ),
            any(
                intent.phase != IntentPhase.COMPLETED
                or (
                    intent.lifecycle in (LifecycleClass.OWNED_JOB, LifecycleClass.SESSION_TURN)
                    and (
                        intent.observation is None
                        or not intent.observation.terminal
                        or not intent.observation.released
                    )
                )
                for intent in state.intents.intents
            ),
            bool(state.settlement.pending),
        )
    )
