"""Frozen settlement event dispatch; lifecycle behavior belongs to independent leaves."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import _adoption, _settlement
from .types.settlement import (
    AdoptionObserved,
    AssessmentSubmitted,
    AttemptSettled,
    OwnershipSettled,
    WinnerProposed,
)

if TYPE_CHECKING:
    from .types.kernel import AreaChange, SettlementContext
    from .types.settlement import SettlementEvent, SettlementState


type Reducer = Callable[
    [SettlementState, SettlementContext, SettlementEvent], AreaChange[SettlementState]
]

# Only this wrapper changes event ownership; leaves preserve sibling-owned fields.
EVENT_TO_SUBAREA: Mapping[type[SettlementEvent], Reducer] = MappingProxyType(
    {
        AssessmentSubmitted: _settlement.advance,
        OwnershipSettled: _settlement.advance,
        AttemptSettled: _settlement.advance,
        WinnerProposed: _adoption.advance,
        AdoptionObserved: _adoption.advance,
    }
)


def settle(
    state: SettlementState, context: SettlementContext, event: SettlementEvent
) -> AreaChange[SettlementState]:
    """Route each closed event variant to its sole owning subarea."""
    reducer: Reducer = EVENT_TO_SUBAREA[type(event)]
    return reducer(state, context, event)


def advance_adoption(
    state: SettlementState, context: SettlementContext, event: SettlementEvent
) -> AreaChange[SettlementState]:
    """Compatibility entry point uses the same exhaustive ownership table."""
    return settle(state, context, event)
