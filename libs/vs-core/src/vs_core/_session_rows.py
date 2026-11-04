"""Session and turn rows of the sessions state, and the waits a session records on them.

One home for storing a session or a turn and for naming what a session waits on, shared by
the session turn, admission and checkpoint transitions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import ContractValidationError, RequestId
from .types.records import phase_open
from .types.sessions import session_may_wait, turn_may_wait
from .types.waits import WaitOn

if TYPE_CHECKING:
    from .types.common import InvocationRef, SessionId
    from .types.kernel import SessionsContext
    from .types.records import Request
    from .types.sessions import InterruptClaim, Invocation, SessionsState, SessionView


def session_row(state: SessionsState, identity: SessionId) -> SessionView | None:
    """The session *identity* in *state*, or None."""
    return next((row for row in state.sessions if row.spec.session_id == identity), None)


def invocation_row(state: SessionsState, ref: InvocationRef) -> Invocation | None:
    """The turn *ref* in *state*, or None."""
    return next((row for row in state.invocations if row.invocation == ref), None)


def replace_session(state: SessionsState, session: SessionView) -> SessionsState:
    """*state* with *session* stored; a session that left its waiting phases drops its wait."""
    if session.wait is not None and not session_may_wait(session.phase):
        session = session.model_copy(update={"wait": None})
    rows = tuple(
        session if row.spec.session_id == session.spec.session_id else row for row in state.sessions
    )
    if session_row(state, session.spec.session_id) is None:
        rows = (*rows, session)
    return state.model_copy(update={"sessions": rows})


def awaiting(
    session: SessionView, context: SessionsContext, *requests: Request | RequestId
) -> SessionView:
    """*session* waiting on *requests* as well as on the ones it named that are still open.

    The transition that sends a request the session now depends on, or that leaves it
    waiting on a request already in flight, names it here, in the same step; nothing later
    infers it. Requests of the old wait that ended drop out; one this step sent and core has
    not recorded yet has not ended. A session left naming nothing carries no wait, which the
    step's wait check reports as a waiting session without one.
    """
    return session.model_copy(update={"wait": waiting_on(session.wait, context, *requests)})


def waiting_on(
    wait: WaitOn | None, context: SessionsContext, *requests: Request | RequestId
) -> WaitOn | None:
    """A wait on *requests* and on the requests of *wait* still open; None if none is left.

    Requests of the old wait that ended drop out; one this step sent and core has not
    recorded yet has not ended. A request named by identity that already ended is not named.
    """
    kept = (
        ()
        if wait is None
        else tuple(request for request in wait.requests if not request_ended(context, request))
    )
    named_now: list[RequestId] = []
    for request in requests:
        if isinstance(request, RequestId):
            if not request_ended(context, request):
                named_now.append(request)
            continue
        if request.request_id is None:
            raise ContractValidationError("request_id", "a wait names a request by its identity")
        named_now.append(request.request_id)
    named = tuple(dict.fromkeys((*kept, *named_now)))
    return WaitOn(requests=named) if named else None


def request_ended(context: SessionsContext, request: RequestId) -> bool:
    """Whether core holds *request* and applied its end; one not yet recorded has not ended."""
    record = context.records.record(request)
    return record is not None and not phase_open(record.phase)


def replace_invocation(state: SessionsState, invocation: Invocation) -> SessionsState:
    """*state* with *invocation* stored; a turn that stopped acquiring or yielding drops its wait."""
    if invocation.wait is not None and not turn_may_wait(invocation):
        invocation = invocation.model_copy(update={"wait": None})
    rows = tuple(
        invocation if row.invocation == invocation.invocation else row for row in state.invocations
    )
    if invocation_row(state, invocation.invocation) is None:
        rows = (*rows, invocation)
    return state.model_copy(update={"invocations": rows})


def replace_claim(state: SessionsState, claim: InterruptClaim) -> SessionsState:
    """*state* with the interruption *claim* stored; a claim that stopped draining drops its wait."""
    if claim.wait is not None and claim.phase != "draining":
        claim = claim.model_copy(update={"wait": None})
    rows = tuple(claim if row.invocation == claim.invocation else row for row in state.interrupts)
    if all(row.invocation != claim.invocation for row in state.interrupts):
        rows = (*rows, claim)
    return state.model_copy(update={"interrupts": rows})


def awaiting_checkpoint(
    state: SessionsState,
    context: SessionsContext,
    ref: InvocationRef,
    *requests: Request | RequestId,
) -> SessionsState:
    """*state* with the yield of *ref* and its draining interruption waiting on *requests* too.

    A turn whose yield is unpublished, and a draining interruption of it, wait on the request
    that retains the turn's checkpoint, or on the inspection of the turn that lets core
    request one. The transition that sends or confirms that request names it here.
    """
    turn = invocation_row(state, ref)
    if turn is not None and turn.pending_suspension is not None:
        wait = waiting_on(turn.wait, context, *requests)
        state = replace_invocation(state, turn.model_copy(update={"wait": wait}))
    claim = next((row for row in state.interrupts if row.invocation == ref), None)
    if claim is not None and claim.phase == "draining":
        wait = waiting_on(claim.wait, context, *requests)
        state = replace_claim(state, claim.model_copy(update={"wait": wait}))
    return state
