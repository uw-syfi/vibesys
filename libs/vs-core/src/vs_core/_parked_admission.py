"""Which events a parked epoch still admits.

A parked epoch ignores ordinary events. It keeps resume, controls, and
decisions, and the session input that delivers a steer the epoch already holds
when that delivery is not recorded yet. ``parked_epoch_drops`` is the only
decision ``consume`` needs: true means the event leaves the epoch unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._run_pause import pause_outlived
from .types.common import RunStatus
from .types.kernel import (
    DecisionSubmitted,
    ProposalSubmitted,
    RunControlEvent,
)
from .types.records import DispatchAuthorized
from .types.sessions import SessionInputReceived

if TYPE_CHECKING:
    from .types.kernel import CoreEvent, CoreState


def parked_epoch_drops(state: CoreState, event: CoreEvent) -> bool:
    """Whether a parked epoch ignores ``event``.

    The epoch admits resume, controls, and decisions. It also admits the
    session input that delivers a steer control it already holds when that input is
    not recorded yet: the control and its delivery are separate events, and a crash
    between them is repaired by a later poll. Dropping the delivery leaves the steer
    recorded with nothing for the next turn to read. A different payload under that
    input's id is admitted so the conflict is reported. Any other input stays out.
    """
    if state.run.status != RunStatus.PARKED or state.run.park is None or pause_outlived(state):
        return False
    if isinstance(
        event,
        RunControlEvent | DispatchAuthorized | DecisionSubmitted | ProposalSubmitted,
    ):
        return False
    return not _repairs_unrecorded_steer(state, event)


def _repairs_unrecorded_steer(state: CoreState, event: CoreEvent) -> bool:
    """Whether ``event`` is the missing delivery of a steer this parked epoch holds.

    The input id is ``steer-{control_id}``, the id ``steer_input_id`` records.
    Core does not import the runtime, so the prefix is repeated here.
    """
    if not isinstance(event, SessionInputReceived):
        return False
    control = next(
        (
            row
            for row in state.run.controls
            if row.action == "steer"
            and row.artifact is not None
            and f"steer-{row.control_id.root}" == event.input.input_id.root
        ),
        None,
    )
    if control is None:
        return False
    recorded = next(
        (row.input for row in state.sessions.inputs if row.input.input_id == event.input.input_id),
        None,
    )
    return recorded is None or recorded != event.input
