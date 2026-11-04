"""Which watches have fired: the conditions that end a wait before its limit.

A ``WhileOpen`` fires when its workspace is no longer open (not acquiring or active, or
already closing). A ``WhileAlive`` fires when the job it names is known to have ended.
A job core has never heard of has not fired: it may simply not be registered yet.
A step whose watch fired is ended by ``decide`` (``Cancel`` or ``Settle`` of ``WITHDRAWN``), and
the fold of that end names the watch (``fired_watch``), as it names a limit that passed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.attempts import AttemptPhase
from .types.until import FailureReason, WhileAlive, WhileOpen

if TYPE_CHECKING:
    from .types.kernel import CoreState
    from .types.records import RequestRecord
    from .types.until import Watch


def _workspace_open(state: CoreState, watch: WhileOpen) -> bool:
    return any(
        attempt.attempt_id == watch.workspace.attempt_id
        and attempt.generation == watch.workspace.generation
        and attempt.closure is None
        and attempt.phase in (AttemptPhase.ACQUIRING, AttemptPhase.ACTIVE)
        for attempt in state.attempts.attempts
    )


def _job_gone(state: CoreState, watch: WhileAlive) -> bool:
    jobs = (*state.evaluation.jobs, *state.evaluation.registered_jobs)
    return any(job.resource_id == watch.job and (job.terminal or job.released) for job in jobs)


def fired(state: CoreState, watch: Watch) -> FailureReason | None:
    """The reason ``watch`` ends its wait now, or ``None`` while the wait should go on."""
    if isinstance(watch, WhileOpen):
        return None if _workspace_open(state, watch) else FailureReason.WORKSPACE_CLOSED
    return FailureReason.OWNER_GONE if _job_gone(state, watch) else None


def fired_watch(state: CoreState, record: RequestRecord) -> tuple[Watch, FailureReason] | None:
    """The first watch of the open step ``record`` that has fired, with the reason it ends the step."""
    for watch in record.until.watches:
        if (reason := fired(state, watch)) is not None:
            return watch, reason
    return None
