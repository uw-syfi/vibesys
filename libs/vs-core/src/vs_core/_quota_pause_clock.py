"""A turn's limit does not run while a quota stop holds the run: the turn is waiting for capacity.

A turn that stops on a provider capacity limit waits inside its dispatch, on the shell's side,
until the run's quota pause ends (the policy's timed resume, or the operator). The wait is not
the turn's work, so it must not spend the turn's limit: a role's ``turn_time`` is how long the
provider may take, and a wait the provider's own reset sets could otherwise outlast it, end the
turn as lost and leave the pause with nothing to resume it.

Core learns of the wait only as the run's quota pause (``run_pause``, owned by ``QUOTA``). While
the run is held by one, every dispatched turn's limit moves by the time that passes, exactly as
host downtime moves an open record's limit (``_request_recovery.open_host_epoch``). The
limit moves only for a turn already dispatched, and only as far as the run deadline, which
still ends a wait that never resumes. A turn running beside the one that waited gets the same
extra time, which is bounded by the pause.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._request_recovery import is_open
from ._run_pause import run_pause
from .types.common import PauseOwner, RunStatus
from .types.records import RequestPhase
from .types.sessions import DispatchTurn, ResumeSessionTurn

if TYPE_CHECKING:
    from .types.kernel import CoreState
    from .types.records import RequestRecord


def credit_quota_pause(state: CoreState, now_at: float) -> CoreState:
    """``state`` with each dispatched turn's limit moved past the time up to ``now_at``.

    Call it before the run's clock moves to ``now_at``, while ``run.now_at`` is still the time
    the last event reached. A run that is not held by a quota pause, or a clock that does not
    move forward, changes nothing.
    """
    run = state.run
    gap = now_at - run.now_at
    if gap <= 0 or run.status != RunStatus.PAUSED:
        return state
    pause = run_pause(run)
    if pause is None or pause.owner is not PauseOwner.QUOTA:
        return state

    def moved(record: RequestRecord) -> RequestRecord:
        if not (
            is_open(record)
            and record.phase == RequestPhase.DISPATCHED
            and isinstance(record.request, DispatchTurn | ResumeSessionTurn)
        ):
            return record
        at = record.until.at + gap
        if record.until.at <= run.deadline_at:
            at = min(at, run.deadline_at)
        if at == record.until.at:
            return record
        return record.model_copy(update={"until": record.until.model_copy(update={"at": at})})

    records = tuple(moved(record) for record in state.records.records)
    if records == state.records.records:
        return state
    held = state.records.model_copy(update={"records": records})
    return state.model_copy(update={"records": held})
