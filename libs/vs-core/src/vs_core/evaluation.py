"""Frozen evaluation event dispatch; lifecycle behavior belongs to independent leaves."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import _continuations, _job_releases, _measurement_reuse, _measurements
from ._comparison import closed_answers, comparisons_settled, moment_of
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
    MeasurementSubmissionEnded,
    MeasurementSubmissionObserved,
    ObservationsDue,
    RegisteredJobObserved,
    RegisteredJobRequested,
    RunJobsCancelRequested,
    TurnSuspended,
)

if TYPE_CHECKING:
    from .types.evaluation import EvaluationEvent, EvaluationState
    from .types.kernel import AreaChange, EvaluationContext, RunState


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
        MeasurementSubmissionEnded: _measurements.submission_ended,
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

    The event's time has already moved the run clock. Waits whose bound it reached freeze
    first, on the job facts they had then, before the event changes them; a wait whose bound
    the event's facts moved into the past freezes right after it (``_wait_clock``).
    """
    due = _continuations.reach_deadlines(state, context)
    reducer: Reducer = EVENT_TO_SUBAREA[type(event)]
    change = reducer(due.state, context, event)
    after = _continuations.reach_deadlines(change.state, context)
    # A job's release is announced to its closing attempt here, whichever subarea recorded it.
    announced = _job_releases.job_release_signals(state, after.state, context)
    # So is every comparison the step settled, however the run that fed it ended, after the
    # step's own results.
    moment = moment_of(context.run)
    settled = comparisons_settled(state, after.state, moment)
    return after.model_copy(
        update={
            "state": closed_answers(after.state, moment),
            "signals": (*due.signals, *change.signals, *after.signals, *announced),
            "requests": (*due.requests, *change.requests, *after.requests),
            "events": (*due.events, *change.events, *after.events, *settled),
        }
    )


def unanswered_measures(run: RunState) -> tuple[MeasurementRequested, ...]:
    """The measurements core accepted and has no request or answer for, as they were asked.

    The kernel admits each again when a resumed host is ready, so none waits for an answer
    nothing will give (`_measurement_reuse.unanswered_measures`).
    """
    return _measurement_reuse.unanswered_measures(run)


def credit_downtime(state: EvaluationState, run: RunState, credit: float) -> EvaluationState:
    """Every open wait with ``credit`` seconds of host downtime added to its limit.

    The kernel calls this when a new host epoch opens, with the downtime the host log proves,
    as it moves every open request record's limit (``records.open_host_epoch``).
    """
    return _continuations.credit_downtime(state, run, credit)
