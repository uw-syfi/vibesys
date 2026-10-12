"""Ending the turn of a checkpoint wait that nothing can resolve, as any ended turn is cleaned up."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import _session_checkpoints
from ._session_turns import _after_turn_cleanup, _session
from .types.common import ContractValidationError
from .types.kernel import AreaChange
from .types.sessions import CheckpointStalled

if TYPE_CHECKING:
    from .types.kernel import SessionsContext
    from .types.sessions import SessionsEvent, SessionsState


def advance_stalled_checkpoint_waits(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """End the turn of a suspension nothing can publish, then clean up as any ended turn does."""
    if not isinstance(event, CheckpointStalled):
        raise ContractValidationError("event.kind", "event is not a yield stall")
    change, ended = _session_checkpoints.end_stalled_checkpoint_waits(state, context, event)
    session = _session(change.state, event.invocation.session_id)
    if ended is None or session is None or session.invocation != event.invocation.invocation_id:
        return change
    cleanup = _after_turn_cleanup(change.state, context, ended, session)
    return AreaChange(
        state=cleanup.state,
        signals=(*change.signals, *cleanup.signals),
        requests=cleanup.requests,
        events=change.events,
    )
