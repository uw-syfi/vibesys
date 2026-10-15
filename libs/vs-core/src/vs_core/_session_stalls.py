"""Ending the waits of sessions and their turns that nothing can carry on any more."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import _session_checkpoints
from ._acquisition_stall import stalled_acquisition
from ._session_rows import awaiting as _awaiting
from ._session_rows import request_ended as _ended
from ._session_scope import inspect_turn
from ._session_state import _current_invocation, _reconciling, _record
from ._session_turns import (
    _after_turn_cleanup,
    _drain_lease,
    _invocation,
    _prepared_completion,
    _release_lost_turn,
    _replace_invocation,
    _replace_session,
    _session,
)
from ._turn_outcomes import is_terminal, turn_result
from ._waits import stalled_acquisitions
from .types.common import ContractValidationError, InvocationRef
from .types.kernel import AreaChange
from .types.sessions import (
    AcquisitionStalled,
    CheckpointStalled,
    CloseSession,
    InputReservationReleased,
    SessionPhase,
    SessionWaitLapsed,
)
from .types.waits import WaitOn

if TYPE_CHECKING:
    from .types.common import RequestId
    from .types.kernel import SessionsContext
    from .types.sessions import SessionsEvent, SessionsState, SessionView


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

    With none left, an unknown session whose turn's acceptance is unsettled releases that
    turn as lost, as when its inspection answers unknown, and waits on the cancellation. An
    unknown session with no unsettled turn and a known lease closes that lease as lost and
    waits on the close. A closing session goes on closing (``_go_on_closing``). Any other
    session (an ambiguous ensure, whose lease core does not know) keeps its wait on ended
    requests. It is then an orphan, and the run halts loudly with it named (``RESOLUTIONS``).
    """
    if not isinstance(event, SessionWaitLapsed):
        raise ContractValidationError("event.kind", "event is not a lapsed session wait")
    session = _session(state, event.session_id)
    if session is None or session.wait is None or event.request_id not in session.wait.requests:
        return AreaChange(state=state)
    if not _ended(context, event.request_id):
        return AreaChange(state=state)
    kept = tuple(request for request in session.wait.requests if not _ended(context, request))
    if not kept:
        return _nothing_left(state, context, session, event.request_id)
    waiting = session.model_copy(update={"wait": WaitOn(requests=kept)})
    return AreaChange(state=_replace_session(state, waiting))


def _nothing_left(
    state: SessionsState, context: SessionsContext, session: SessionView, lapsed: RequestId
) -> AreaChange[SessionsState]:
    """Move a session whose every awaited request ended without moving it, if anything can."""
    if session.phase == SessionPhase.CLOSING:
        return _go_on_closing(state, context, session, lapsed)
    if session.phase != SessionPhase.UNKNOWN:
        return AreaChange(state=state)
    turn = (
        None
        if session.invocation is None
        else _invocation(
            state,
            InvocationRef(
                session_id=session.spec.session_id,
                invocation_id=session.invocation,
                generation=session.generation,
            ),
        )
    )
    if turn is not None and turn.phase == SessionPhase.UNKNOWN:
        return _release_lost_turn(state, context, turn.invocation)
    if session.resource_id is not None:
        # Its turn ended but whether it accepted its inputs is unknown, and nothing will
        # ask again: close the known lease as lost and wait on the close. Only the
        # close's answer releases the lease, never the absence of an answer.
        return _drain_lease(state, context, session, lapsed)
    return AreaChange(state=state)


def _go_on_closing(
    state: SessionsState, context: SessionsContext, session: SessionView, lapsed: RequestId
) -> AreaChange[SessionsState]:
    """Carry on a close whose cancel or close request ended (at its limit) without an answer.

    A cancel that ended unanswered leaves the turn's state unknown, so the turn is inspected
    once; its answer ends the turn, and an ended turn closes its session. A turn still not
    ended after that is never released, so its session keeps its wait. A close that ended
    unanswered is sent once more under a new identity. The ended one is blocked, so no answer
    of it is taken any more, and it may never have gone out (it waited for a slot until the
    run deadline). Closing again is safe whether or not it ran: the episode's fence keeps its
    first closer, and a release keeps the conversation's checkpoint. When that second close
    ends unanswered as well, the session keeps its wait, and the run halts loudly with it
    named (``RESOLUTIONS``).
    """
    turn = _current_invocation(state, session)
    if turn is not None and not is_terminal(turn):
        # A turn that may still be running is never released: its session is not closed.
        inspection = inspect_turn(context, turn)
        if _record(context, inspection.request_id) is not None:
            return AreaChange(state=state)
        return AreaChange(
            state=_replace_session(state, _awaiting(session, context, inspection)),
            requests=(_reconciling(context, inspection),),
        )
    closes = [
        row.request_id
        for row in context.records.records
        if row.request_id in session.pending_records and isinstance(row.request, CloseSession)
    ]
    if len(closes) > 1:
        return AreaChange(state=state)
    return _drain_lease(state, context, session, closes[0] if closes else lapsed)
