"""Frozen evaluation event dispatch; lifecycle behavior belongs to independent leaves."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import _continuations, _job_releases, _measurements
from .types.evaluation import (
    AgentMeasurementRequested,
    ContinuationJobsChanged,
    ContinuationReopenRequested,
    ContinuationRetireRequested,
    ContinuationScopeReopened,
    DeadlineReached,
    JobObserved,
    JobsDrainRequested,
    JobTerminationRequested,
    JobWaitLapsed,
    MeasurementRequested,
    MeasurementSubmissionObserved,
    ObservationsDue,
    RegisteredJobObserved,
    RegisteredJobRequested,
    RunJobsCancelRequested,
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
        RunJobsCancelRequested: _measurements.advance,
        MeasurementSubmissionObserved: _measurements.advance,
        ContinuationJobsChanged: _continuations.advance,
        ContinuationRetireRequested: _continuations.advance,
        ContinuationReopenRequested: _continuations.advance,
        ContinuationScopeReopened: _continuations.advance,
        RegisteredJobObserved: _measurements.advance,
        RegisteredJobRequested: _measurements.advance,
        MeasurementRequested: _measurements.advance,
        AgentMeasurementRequested: _measurements.advance,
        JobObserved: _measurements.advance,
        TurnSuspended: _continuations.advance,
        DeadlineReached: _continuations.advance,
        ObservationsDue: _measurements.advance,
        JobWaitLapsed: _measurements.advance,
    }
)


def advance_evaluation(
    state: EvaluationState, context: EvaluationContext, event: EvaluationEvent
) -> AreaChange[EvaluationState]:
    """Route each closed event variant to its sole owning subarea.

    The event's time has already moved the run clock. Waits whose deadline it reached
    freeze first, on the job facts they had at the deadline, before the event changes them.
    The kernel then charges the queue time the change ended (``_wait_clock``).
    """
    due = _continuations.reach_deadlines(state, context)
    reducer: Reducer = EVENT_TO_SUBAREA[type(event)]
    change = reducer(due.state, context, event)
    # A job's release is announced to its closing attempt here, whichever subarea recorded it.
    announced = _job_releases.job_release_signals(state, change.state, context)
    return change.model_copy(
        update={
            "signals": (*due.signals, *change.signals, *announced),
            "requests": (*due.requests, *change.requests),
            "events": (*due.events, *change.events),
        }
    )
