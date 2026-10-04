"""Frozen intents event dispatch; lifecycle behavior belongs to independent leaves."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import _intent_ledger, _intent_recovery
from .types.intents import (
    DecisionDependencyResolved,
    DispatchAuthorized,
    OperationRetireRequested,
    ReconciliationDeadline,
    RecoveryReady,
    RecoveryStarted,
    RequestObserved,
    RequestPrepared,
)

if TYPE_CHECKING:
    from .types.intents import IntentsEvent, IntentsState
    from .types.kernel import AreaChange, IntentsContext


type Reducer = Callable[[IntentsState, IntentsContext, IntentsEvent], AreaChange[IntentsState]]


def _observation(
    state: IntentsState, context: IntentsContext, event: IntentsEvent
) -> AreaChange[IntentsState]:
    """Commit request-ledger facts and descendant/recovery facts atomically.

    The same canonical observation reaches both owners because inspected child
    facts are not persisted in the request ledger. Recovery preserves ledger
    fields; the wrapper merges outputs before the kernel registers any request.
    """
    ledger = _intent_ledger.advance(state, context, event)
    recovery = _intent_recovery.advance(ledger.state, context, event)
    return recovery.model_copy(
        update={
            "signals": (*ledger.signals, *recovery.signals),
            "requests": (*ledger.requests, *recovery.requests),
            "events": (*ledger.events, *recovery.events),
        }
    )


# Only this wrapper changes event ownership; leaves preserve sibling-owned fields.
EVENT_TO_SUBAREA: Mapping[type[IntentsEvent], Reducer] = MappingProxyType(
    {
        RequestPrepared: _intent_ledger.advance,
        DispatchAuthorized: _intent_ledger.advance,
        RequestObserved: _observation,
        DecisionDependencyResolved: _intent_ledger.advance,
        OperationRetireRequested: _intent_ledger.advance,
        RecoveryStarted: _intent_recovery.advance,
        RecoveryReady: _intent_recovery.advance,
        ReconciliationDeadline: _intent_recovery.advance,
    }
)


def advance_intent(
    state: IntentsState, context: IntentsContext, event: IntentsEvent
) -> AreaChange[IntentsState]:
    """Route closed events to their owners, sharing observations atomically."""
    reducer: Reducer = EVENT_TO_SUBAREA[type(event)]
    return reducer(state, context, event)


def recover(
    state: IntentsState, context: IntentsContext, event: IntentsEvent
) -> AreaChange[IntentsState]:
    """Apply recovery to committed ledger facts, preserving all ledger fields.

    Observation callers must first commit the query and canonical root facts.
    The kernel uses ``advance_intent`` to commit both owners atomically; this
    recovery-only entry point also permits independent Intents B adoption.
    """
    return _intent_recovery.advance(state, context, event)
