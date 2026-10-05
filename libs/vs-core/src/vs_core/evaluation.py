"""Frozen evaluation event dispatch; lifecycle behavior belongs to independent leaves."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import _continuations, _measurements
from .types.evaluation import (
    ContinuationJobsChanged,
    ContinuationReopenRequested,
    ContinuationRetireRequested,
    ContinuationScopeReopened,
    DeadlineReached,
    JobObserved,
    JobsDrainRequested,
    JobTerminationRequested,
    MeasurementRequested,
    MeasurementSubmissionObserved,
    ObservationsDue,
    RegisteredJobObserved,
    RegisteredJobRequested,
    TurnSuspended,
)

if TYPE_CHECKING:
    from .types.evaluation import EvaluationEvent, EvaluationState
    from .types.kernel import AreaChange, EvaluationContext


type Reducer = Callable[
    [EvaluationState, EvaluationContext, EvaluationEvent], AreaChange[EvaluationState]
]

# Only this wrapper changes event ownership; leaves preserve sibling-owned fields.
EVENT_TO_SUBAREA: Mapping[type[EvaluationEvent], Reducer] = MappingProxyType(
    {
        JobTerminationRequested: _measurements.advance,
        JobsDrainRequested: _measurements.advance,
        MeasurementSubmissionObserved: _measurements.advance,
        ContinuationJobsChanged: _continuations.advance,
        ContinuationRetireRequested: _continuations.advance,
        ContinuationReopenRequested: _continuations.advance,
        ContinuationScopeReopened: _continuations.advance,
        RegisteredJobObserved: _measurements.advance,
        RegisteredJobRequested: _measurements.advance,
        MeasurementRequested: _measurements.advance,
        JobObserved: _measurements.advance,
        TurnSuspended: _continuations.advance,
        DeadlineReached: _continuations.advance,
        ObservationsDue: _measurements.advance,
    }
)


def advance_evaluation(
    state: EvaluationState, context: EvaluationContext, event: EvaluationEvent
) -> AreaChange[EvaluationState]:
    """Route each closed event variant to its sole owning subarea."""
    reducer: Reducer = EVENT_TO_SUBAREA[type(event)]
    return reducer(state, context, event)
