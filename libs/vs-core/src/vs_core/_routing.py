"""Exhaustive event routing in one location, with fixed propagation order."""

from __future__ import annotations

from typing import assert_never

from ._registry import ContractError
from .types.attempts import (
    AttemptAdmitted,
    InvocationCheckpointed,
    RetentionRequired,
    RetireRequested,
    WorkspaceObserved,
)
from .types.common import Area
from .types.evaluation import DeadlineReached, JobObserved, MeasurementRequested, TurnSuspended
from .types.intents import (
    DispatchAuthorized,
    ReconciliationDeadline,
    RecoveryStarted,
    RequestObserved,
    RequestPrepared,
)
from .types.kernel import CoreEvent, DecisionSubmitted, RunControlEvent, Signal
from .types.scheduling import (
    AdmissionControl,
    AdmitAttempt,
    AttemptReady,
    AttemptRequested,
    ClockAdvanced,
    CloseAdmission,
    RunDrained,
    SlotReleased,
)
from .types.sessions import (
    InterruptRequested,
    SessionObserved,
    SteerReceived,
    TurnObserved,
    TurnRequested,
)
from .types.settlement import (
    AdoptionObserved,
    AssessmentSubmitted,
    AttemptSettled,
    OwnershipSettled,
    WinnerProposed,
)

SIGNAL_ORDER = (
    Area.SCHEDULING,
    Area.ATTEMPTS,
    Area.SESSIONS,
    Area.EVALUATION,
    Area.SETTLEMENT,
    Area.INTENTS,
)


def event_area(event: CoreEvent | Signal) -> Area:
    """Every published event has exactly one owning reducer."""
    match event:
        case (
            AttemptRequested()
            | AttemptReady()
            | SlotReleased()
            | ClockAdvanced()
            | AdmissionControl()
            | AdmitAttempt()
            | CloseAdmission()
            | RunDrained()
        ):
            return Area.SCHEDULING
        case (
            AttemptAdmitted()
            | WorkspaceObserved()
            | RetireRequested()
            | InvocationCheckpointed()
            | RetentionRequired()
        ):
            return Area.ATTEMPTS
        case (
            TurnRequested()
            | TurnObserved()
            | SessionObserved()
            | SteerReceived()
            | InterruptRequested()
        ):
            return Area.SESSIONS
        case MeasurementRequested() | JobObserved() | TurnSuspended() | DeadlineReached():
            return Area.EVALUATION
        case (
            AssessmentSubmitted()
            | OwnershipSettled()
            | WinnerProposed()
            | AdoptionObserved()
            | AttemptSettled()
        ):
            return Area.SETTLEMENT
        case (
            RequestPrepared()
            | DispatchAuthorized()
            | RequestObserved()
            | RecoveryStarted()
            | ReconciliationDeadline()
        ):
            return Area.INTENTS
        case DecisionSubmitted() | RunControlEvent():
            raise ContractError(("event",), "kernel event requires translation")
        case _:
            assert_never(event)
