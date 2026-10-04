"""Frozen attempts event dispatch; lifecycle behavior belongs to independent leaves."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import _attempt_acquisition, _attempt_retirement
from .types.attempts import (
    AttemptAdmitted,
    AttemptChargeRefundRequested,
    AttemptEvaluationExhausted,
    AttemptEvaluationHistoryUpdated,
    AttemptReacquireRequested,
    AttemptRegistered,
    AttemptSetupFailed,
    InitialSessionsFailed,
    InitialSessionsReady,
    InvocationChargeRequested,
    InvocationCheckpointed,
    InvocationCheckpointRequested,
    InvocationEnded,
    ReacquisitionReady,
    ReleaseDependencyBlocked,
    ReleaseDependencyObserved,
    RetentionRequired,
    RetireRequested,
    RevisionOperationObserved,
    RevisionOperationRequested,
    ScopeAdmissionReopened,
    ScopeReopenAdmitted,
    ScopeReopenRequested,
    WorkspaceObserved,
)

if TYPE_CHECKING:
    from .types.attempts import AttemptsEvent, AttemptsState
    from .types.kernel import AreaChange, AttemptsContext


type Reducer = Callable[[AttemptsState, AttemptsContext, AttemptsEvent], AreaChange[AttemptsState]]

# Only this wrapper changes event ownership; leaves preserve sibling-owned fields.
EVENT_TO_SUBAREA: Mapping[type[AttemptsEvent], Reducer] = MappingProxyType(
    {
        AttemptRegistered: _attempt_acquisition.advance,
        AttemptEvaluationExhausted: _attempt_acquisition.advance,
        AttemptEvaluationHistoryUpdated: _attempt_acquisition.advance,
        AttemptReacquireRequested: _attempt_acquisition.advance,
        InitialSessionsReady: _attempt_acquisition.advance,
        InitialSessionsFailed: _attempt_acquisition.advance,
        InvocationChargeRequested: _attempt_acquisition.advance,
        AttemptSetupFailed: _attempt_acquisition.advance,
        InvocationEnded: _attempt_acquisition.advance,
        InvocationCheckpointRequested: _attempt_acquisition.advance,
        AttemptChargeRefundRequested: _attempt_acquisition.advance,
        ScopeReopenRequested: _attempt_retirement.advance,
        ScopeReopenAdmitted: _attempt_retirement.advance,
        ReacquisitionReady: _attempt_retirement.advance,
        ScopeAdmissionReopened: _attempt_retirement.advance,
        ReleaseDependencyObserved: _attempt_retirement.advance,
        ReleaseDependencyBlocked: _attempt_retirement.advance,
        AttemptAdmitted: _attempt_acquisition.advance,
        WorkspaceObserved: _attempt_acquisition.advance,
        InvocationCheckpointed: _attempt_acquisition.advance,
        RevisionOperationRequested: _attempt_acquisition.advance,
        RevisionOperationObserved: _attempt_acquisition.advance,
        RetireRequested: _attempt_retirement.advance,
        RetentionRequired: _attempt_retirement.advance,
    }
)


def advance_attempt(
    state: AttemptsState, context: AttemptsContext, event: AttemptsEvent
) -> AreaChange[AttemptsState]:
    """Route each closed event variant to its sole owning subarea."""
    reducer: Reducer = EVENT_TO_SUBAREA[type(event)]
    return reducer(state, context, event)
