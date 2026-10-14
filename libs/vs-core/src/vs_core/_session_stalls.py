"""Ending the waits of sessions and their turns that nothing can carry on any more."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import _session_checkpoints
from ._acquisition_stall import stalled_acquisition
from ._session_rows import request_ended as _ended
from ._session_turns import (
    _after_turn_cleanup,
    _invocation,
    _prepared_completion,
    _replace_invocation,
    _replace_session,
    _session,
)
from ._turn_outcomes import turn_result
from ._waits import stalled_acquisitions
from .types.common import (
    ContractValidationError,
)
from .types.kernel import AreaChange
from .types.sessions import (
    AcquisitionStalled,
    CheckpointStalled,
    InputReservationReleased,
    SessionPhase,
    SessionWaitLapsed,
)
from .types.waits import WaitOn

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


def advance_stalled_acquisitions(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """End a turn that is acquiring with nothing left to carry it: cancelled, with the reason.

    The turn becomes terminal with a synthesized cancelled observation (nothing ran, so nothing
    is left to release), so its input
    reservation is released and its strategy hears a ``TurnResult`` whose detail says the
    acquisition stalled; its decision completes so dependents do not wait on it, and its
    session can take a new turn. A stall naming a turn that still has a producer, or that is
    no longer acquiring, changes nothing.
    """
    if not isinstance(event, AcquisitionStalled):
        raise ContractValidationError("event.kind", "event is not an acquisition stall")
    invocation = _invocation(state, event.invocation)
    if invocation is None or event.invocation not in stalled_acquisitions(state, context.records):
        return AreaChange(state=state)
    observation = stalled_acquisition(invocation, context.run.now_at)
    ended = invocation.model_copy(
        update={"phase": SessionPhase.TERMINAL, "observation": observation}
    )
    state = _replace_invocation(state, ended)
    session = _session(state, event.invocation.session_id)
    if (
        session is not None
        and session.invocation == event.invocation.invocation_id
        and session.phase not in (SessionPhase.CLOSING, SessionPhase.TERMINAL)
    ):
        state = _replace_session(state, session.model_copy(update={"phase": SessionPhase.IDLE}))
    return AreaChange(
        state=state,
        signals=(
            InputReservationReleased(invocation=event.invocation, observation=observation),
            *_prepared_completion(context, invocation),
        ),
        events=(turn_result(event.invocation, observation),),
    )


def advance_lapsed_session_waits(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """A request the session waits on ended without moving it: wait on the others still open.

    With none left, the session keeps its wait on ended requests. It is then an orphan, and
    the run halts loudly with it named (``RESOLUTIONS``): a session whose last request ended
    without proof may hold a lease or a running turn core cannot account for, and nothing yet
    decides who releases it.
    """
    if not isinstance(event, SessionWaitLapsed):
        raise ContractValidationError("event.kind", "event is not a lapsed session wait")
    session = _session(state, event.session_id)
    if session is None or session.wait is None or event.request_id not in session.wait.requests:
        return AreaChange(state=state)
    if not _ended(context, event.request_id):
        return AreaChange(state=state)
    kept = tuple(request for request in session.wait.requests if not _ended(context, request))
    if not kept and session.phase == SessionPhase.UNKNOWN:
        # An unknown session whose inspection ended unanswered waits on nothing core can name.
        return AreaChange(state=_replace_session(state, session.model_copy(update={"wait": None})))
    if not kept:
        return AreaChange(state=state)
    waiting = session.model_copy(update={"wait": WaitOn(requests=kept)})
    return AreaChange(state=_replace_session(state, waiting))
