"""Frozen sessions event dispatch; lifecycle behavior belongs to independent leaves."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import _session_inputs, _session_turns
from ._proofs import Proven, committed_stop
from .types.kernel import AreaChange
from .types.sessions import (
    InputAcceptanceObserved,
    InputReservationReleased,
    InputReservationRequested,
    InterruptRequested,
    InvocationCancellationRequested,
    InvocationChargeRefunded,
    InvocationChargesAuthorized,
    InvocationCheckpointAvailable,
    RegisteredTurnRequested,
    RunInvocationCheckpointObserved,
    RunInvocationCheckpointRequested,
    RunSessionsDrainRequested,
    SessionDrainRequested,
    SessionInputReceived,
    SessionObserved,
    SessionsAcquireRequested,
    SteerReceived,
    TurnInputsReserved,
    TurnObserved,
    TurnRequested,
)

if TYPE_CHECKING:
    from .types.kernel import SessionsContext
    from .types.sessions import SessionsEvent, SessionsState


type Reducer = Callable[[SessionsState, SessionsContext, SessionsEvent], AreaChange[SessionsState]]


def _shared_observation(
    state: SessionsState,
    context: SessionsContext,
    event: SessionsEvent,
    input_reducer: Reducer = _session_inputs.advance,
) -> AreaChange[SessionsState]:
    """Share checkpoint/drain facts with lease and input authorities atomically.

    Turns preserves inputs and interruption claims; Inputs preserves session,
    invocation, acquisition and charge fields. Drain cancels eligible inputs,
    while parking preserves them under the same canonical retirement authority.
    """
    turn_reducer = (
        _session_turns.advance_run_authority
        if isinstance(event, RunSessionsDrainRequested)
        else _session_turns.advance
    )
    turns = turn_reducer(state, context, event)
    if isinstance(event, RunSessionsDrainRequested):
        # Run-scoped occurrences have no owner closure to drain under; finish_run
        # drops whatever is left with RUN_TERMINAL once ownership is confirmed gone.
        return turns
    inputs = input_reducer(turns.state, context, event)
    return inputs.model_copy(
        update={
            "signals": (*turns.signals, *inputs.signals),
            "requests": (*turns.requests, *inputs.requests),
            "events": (*turns.events, *inputs.events),
        }
    )


def _observed_after_stop(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Keep a committed Stop's drain applying to run-owned sessions as they become closable.

    The drain request is a one-shot signal, so a session whose creation is answered, or
    whose cancelled turn ends, after it ran would otherwise stay open and keep the
    closing run from becoming terminal. Re-applying the drain to the state such an
    observation leaves behind is idempotent: it only adds what is not yet pending.
    """
    change = _session_turns.advance(state, context, event)
    stop = committed_stop(context.run)
    if not isinstance(stop, Proven):
        return change
    drained = _session_turns.advance_run_authority(
        change.state,
        context,
        RunSessionsDrainRequested(scope=stop.value.scope, authority=stop.value.decision_id),
    )
    return AreaChange(
        state=drained.state,
        requests=(*change.requests, *drained.requests),
        signals=(*change.signals, *drained.signals),
        events=(*change.events, *drained.events),
    )


# Only this wrapper changes event ownership; leaves preserve sibling-owned fields.
EVENT_TO_SUBAREA: Mapping[type[SessionsEvent], Reducer] = MappingProxyType(
    {
        SessionsAcquireRequested: _session_turns.advance,
        RunInvocationCheckpointRequested: _session_turns.advance_run_authority,
        RunInvocationCheckpointObserved: _session_turns.advance_run_authority,
        RunSessionsDrainRequested: _shared_observation,
        InvocationChargesAuthorized: _session_turns.advance,
        InvocationCancellationRequested: _session_turns.advance,
        TurnInputsReserved: _session_turns.advance,
        SessionDrainRequested: _shared_observation,
        InvocationCheckpointAvailable: _shared_observation,
        SessionInputReceived: _session_inputs.advance,
        InputReservationRequested: _session_inputs.advance,
        InputAcceptanceObserved: _session_inputs.advance,
        InputReservationReleased: _session_inputs.advance,
        InvocationChargeRefunded: _session_inputs.advance,
        RegisteredTurnRequested: _session_turns.advance,
        TurnRequested: _session_turns.advance,
        TurnObserved: _observed_after_stop,
        SessionObserved: _observed_after_stop,
        SteerReceived: _session_inputs.advance,
        InterruptRequested: _session_inputs.advance,
    }
)


def advance_session(
    state: SessionsState,
    context: SessionsContext,
    event: SessionsEvent,
    *,
    input_reducer: Reducer | None = None,
) -> AreaChange[SessionsState]:
    """Route events to their owner, with an explicit Inputs implementation.

    Turn authority is never replaced. Shared facts advance Turns first, then the
    selected Inputs implementation against its resulting immutable state.
    """
    reducer: Reducer = EVENT_TO_SUBAREA[type(event)]
    if input_reducer is not None:
        if reducer is _shared_observation:
            return _shared_observation(state, context, event, input_reducer)
        if reducer is _session_inputs.advance:
            reducer = input_reducer
    return reducer(state, context, event)


def finish_run(state: SessionsState, context: SessionsContext) -> AreaChange[SessionsState]:
    """Finalize input receipts through Inputs after positive ownership cleanup."""
    return _session_inputs.finish_run(state, context)
