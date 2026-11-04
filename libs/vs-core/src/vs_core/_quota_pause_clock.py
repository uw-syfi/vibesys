"""A turn's limit does not run while a quota stop holds the run: the turn is waiting for capacity.

A turn that stops on a provider capacity limit waits inside its dispatch, on the shell's side,
until the run's quota pause ends (the policy's timed resume, or the operator). The wait is not
the turn's work, so it must not spend the turn's limit: a role's ``turn_time`` is how long the
provider may take, and a wait the provider's own reset sets could otherwise outlast it, end the
turn as lost and leave the pause with nothing to resume it.

Core learns of the wait only as the run's quota pause (``run_pause``, owned by ``QUOTA``). While
the run is held by one, every dispatched turn's limit moves by the time that passes, exactly as
the run clock leaves host downtime out. The
limit moves only for a turn already dispatched, and only as far as the run deadline, which
still ends a wait that never resumes. A turn running beside the one that waited gets the same
extra time, which is bounded by the pause.

A turn the run's capacity gate reports it holds (``capacity_hold``) is skipped: the gate parks
it for the pause and ``_turn_holds`` credits that wait from the report, so moving its limit here
as well would credit the pause twice.

The step's own limit is kept in two places, the record's and the attempts' (``StepAttempts.
deadline_at``, the limit a step returned to unstarted takes back, see ``_step_attempts.unstarted``).
A credit moves both, in one function, so a step that returns to unstarted after the wait (an
interrupted attempt, a raise, a crash) keeps it: a credit written to the record alone was given
back at the return, and ``decide`` ended the turn as timed out against the limit it had before the
pause. A cap on one attempt (``_turn_permit``) moves the record alone, by design.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._run_pause import run_pause
from .types.common import PauseOwner, RunStatus
from .types.records import RequestPhase, is_open
from .types.sessions import DispatchTurn, ResumeSessionTurn

if TYPE_CHECKING:
    from .types.kernel import CoreState
    from .types.records import RequestRecord
    from .types.steps import StepAttempts


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

    def credited(record: RequestRecord) -> bool:
        return (
            is_open(record)
            and record.phase == RequestPhase.DISPATCHED
            and isinstance(record.request, DispatchTurn | ResumeSessionTurn)
            and not (record.capacity_hold is not None and record.capacity_hold.holding)
        )

    def later(at: float) -> float:
        """``at`` moved by the gap, as far as the run deadline when it was not already past it."""
        moved = at + gap
        return min(moved, run.deadline_at) if at <= run.deadline_at else moved

    def moved(record: RequestRecord) -> RequestRecord:
        at = later(record.until.at)
        if not credited(record) or at == record.until.at:
            return record
        return record.model_copy(update={"until": record.until.model_copy(update={"at": at})})

    tags = {record.request_id for record in state.records.records if credited(record)}

    def kept(row: StepAttempts) -> StepAttempts:
        if row.tag not in tags or row.deadline_at is None:
            return row
        return row.model_copy(update={"deadline_at": later(row.deadline_at)})

    records = tuple(moved(record) for record in state.records.records)
    attempts = tuple(kept(row) for row in state.steps.attempts)
    if records == state.records.records and attempts == state.steps.attempts:
        return state
    held = state.records.model_copy(update={"records": records})
    steps = state.steps.model_copy(update={"attempts": attempts})
    return state.model_copy(update={"records": held, "steps": steps})
