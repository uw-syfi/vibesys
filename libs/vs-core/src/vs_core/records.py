"""Frozen records event dispatch; lifecycle behavior belongs to independent leaves."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import _request_ledger, _request_recovery
from .types.kernel import AreaChange
from .types.records import (
    BlockedRecordStalled,
    DispatchAuthorized,
    OperationRetireRequested,
    ReconciliationDeadline,
    RecoveryReady,
    RecoveryStarted,
    RequestObserved,
    RequestPrepared,
    RequestUndispatchable,
    RetryDue,
)

if TYPE_CHECKING:
    from .types.kernel import RecordsContext, RunState
    from .types.records import RecordsEvent, RecordsState


type Reducer = Callable[[RecordsState, RecordsContext, RecordsEvent], AreaChange[RecordsState]]


def _observation(
    state: RecordsState, context: RecordsContext, event: RecordsEvent
) -> AreaChange[RecordsState]:
    """Commit request-ledger facts and descendant/recovery facts atomically.

    The same canonical observation reaches both owners because inspected child
    facts are not persisted in the request ledger. Recovery preserves ledger
    fields; the wrapper merges outputs before the kernel registers any request.
    """
    if isinstance(event, RequestObserved) and _request_ledger.is_late(state, event):
        return AreaChange(state=state)
    ledger = _request_ledger.advance(state, context, event)
    if isinstance(event, RequestObserved):
        event = _request_ledger.canonical(ledger.state, event)
    recovery = _request_recovery.advance(ledger.state, context, event)
    return recovery.model_copy(
        update={
            "signals": (*ledger.signals, *recovery.signals),
            "requests": (*ledger.requests, *recovery.requests),
            "events": (*ledger.events, *recovery.events),
        }
    )


# Only this wrapper changes event ownership; leaves preserve sibling-owned fields.
EVENT_TO_SUBAREA: Mapping[type[RecordsEvent], Reducer] = MappingProxyType(
    {
        RequestPrepared: _request_ledger.advance,
        DispatchAuthorized: _request_ledger.advance,
        RequestObserved: _observation,
        OperationRetireRequested: _request_ledger.advance,
        RecoveryStarted: _request_recovery.advance,
        RecoveryReady: _request_recovery.advance,
        ReconciliationDeadline: _request_recovery.advance,
        RetryDue: _request_ledger.advance,
        RequestUndispatchable: _request_ledger.advance,
        BlockedRecordStalled: _request_ledger.advance,
    }
)


def advance_record(
    state: RecordsState, context: RecordsContext, event: RecordsEvent
) -> AreaChange[RecordsState]:
    """Route closed events to their owners, sharing observations atomically."""
    reducer: Reducer = EVENT_TO_SUBAREA[type(event)]
    return reducer(state, context, event)


def recover(
    state: RecordsState, context: RecordsContext, event: RecordsEvent
) -> AreaChange[RecordsState]:
    """Apply recovery to committed ledger facts, preserving all ledger fields.

    Observation callers must first commit the query and canonical root facts.
    The kernel uses ``advance_record`` to commit both owners atomically; this
    recovery-only entry point also permits independent Records B adoption.
    """
    return _request_recovery.advance(state, context, event)


def credit_downtime(state: RecordsState, run: RunState, event: RecoveryStarted) -> RecordsState:
    """Open records' limits moved past the downtime ``event`` ends (before the clock moves)."""
    return _request_recovery.credit_downtime(state, run, event)
