"""Exhaustive event routing in one location, with fixed propagation order."""

from __future__ import annotations

from typing import assert_never

from ._registry import ContractError
from .types.attempts import (
    AttemptAdmitted,
    InvocationCheckpointed,
    RetentionRequired,
    RetireRequested,
    RevisionOperationObserved,
    RevisionOperationRequested,
    WorkspaceObserved,
)
from .types.common import Area
from .types.evaluation import (
    DeadlineReached,
    JobObserved,
    MeasurementRequested,
    RegisteredJobObserved,
    RegisteredJobRequested,
    TurnSuspended,
)
from .types.intents import (
    DispatchAuthorized,
    OperationRetireRequested,
    ReconciliationDeadline,
    RecoveryStarted,
    RequestObserved,
    RequestPrepared,
)
from .types.kernel import (
    CoreEvent,
    DecisionCompleted,
    DecisionSubmitted,
    ProposalSubmitted,
    RunControlEvent,
    Signal,
)
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
    RegisteredTurnRequested,
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
            RevisionOperationObserved()
            | RevisionOperationRequested()
            | AttemptAdmitted()
            | WorkspaceObserved()
            | RetireRequested()
            | InvocationCheckpointed()
            | RetentionRequired()
        ):
            return Area.ATTEMPTS
        case (
            RegisteredTurnRequested()
            | TurnRequested()
            | TurnObserved()
            | SessionObserved()
            | SteerReceived()
            | InterruptRequested()
        ):
            return Area.SESSIONS
        case (
            RegisteredJobObserved()
            | RegisteredJobRequested()
            | MeasurementRequested()
            | JobObserved()
            | TurnSuspended()
            | DeadlineReached()
        ):
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
            | OperationRetireRequested()
            | DecisionCompleted()
        ):
            return Area.INTENTS
        case DecisionSubmitted() | ProposalSubmitted() | RunControlEvent():
            raise ContractError(("event",), "kernel event requires translation")
        case _:
            assert_never(event)
