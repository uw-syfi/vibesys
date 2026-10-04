"""Frozen sessions event dispatch; lifecycle behavior belongs to independent leaves."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import _session_inputs, _session_turns
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
    from .types.kernel import AreaChange, SessionsContext
    from .types.sessions import SessionsEvent, SessionsState


type Reducer = Callable[[SessionsState, SessionsContext, SessionsEvent], AreaChange[SessionsState]]


def _checkpoint_available(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Publish one checkpoint proof to turns and interruption claims atomically."""
    turns = _session_turns.advance(state, context, event)
    inputs = _session_inputs.advance(turns.state, context, event)
    return inputs.model_copy(
        update={
            "signals": (*turns.signals, *inputs.signals),
            "requests": (*turns.requests, *inputs.requests),
            "events": (*turns.events, *inputs.events),
        }
    )


# Only this wrapper changes event ownership; leaves preserve sibling-owned fields.
EVENT_TO_SUBAREA: Mapping[type[SessionsEvent], Reducer] = MappingProxyType(
    {
        SessionsAcquireRequested: _session_turns.advance,
        InvocationChargesAuthorized: _session_turns.advance,
        InvocationCancellationRequested: _session_turns.advance,
        TurnInputsReserved: _session_turns.advance,
        SessionDrainRequested: _session_turns.advance,
        InvocationCheckpointAvailable: _checkpoint_available,
        SessionInputReceived: _session_inputs.advance,
        InputReservationRequested: _session_inputs.advance,
        InputAcceptanceObserved: _session_inputs.advance,
        InputReservationReleased: _session_inputs.advance,
        InvocationChargeRefunded: _session_inputs.advance,
        RegisteredTurnRequested: _session_turns.advance,
        TurnRequested: _session_turns.advance,
        TurnObserved: _session_turns.advance,
        SessionObserved: _session_turns.advance,
        SteerReceived: _session_inputs.advance,
        InterruptRequested: _session_inputs.advance,
    }
)


def advance_session(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Route each closed event variant to its sole owning subarea."""
    reducer: Reducer = EVENT_TO_SUBAREA[type(event)]
    return reducer(state, context, event)
