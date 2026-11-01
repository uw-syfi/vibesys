"""Which watches have fired: the conditions that end a wait before its limit.

A ``WhileOpen`` fires when its workspace is no longer open (not acquiring or active, or
already closing). A ``WhileAlive`` fires when the job it names is known to have ended.
A job core has never heard of has not fired: it may simply not be registered yet.
Evaluated at quiescence, so a watch ends a wait in the same step that closed the
workspace or ended the job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.attempts import AttemptPhase
from .types.records import ReconciliationDeadline, is_open
from .types.until import FailureReason, WhileAlive, WhileOpen

if TYPE_CHECKING:
    from .types.kernel import CoreState
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


def watch_deadlines(state: CoreState) -> tuple[ReconciliationDeadline, ...]:
    """One deadline event per open record with a fired watch, carrying that watch's reason."""
    found = []
    for record in state.records.records:
        if not is_open(record):
            continue
        reason = next(
            (reason for watch in record.until.watches if (reason := fired(state, watch))),
            None,
        )
        if reason is not None:
            found.append(
                ReconciliationDeadline(
                    request_id=record.request_id, now_at=state.run.now_at, reason=reason
                )
            )
    return tuple(found)
