"""Exhaustive event routing in one location, with fixed propagation order."""

from __future__ import annotations

from typing import assert_never

from ._registry import ContractError
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
from .types.common import Area
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
    RegisteredJobObserved,
    RegisteredJobRequested,
    TurnSuspended,
)
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
    AdoptionFenceLifted,
    AttemptReady,
    AttemptReopenRequested,
    AttemptRequested,
    ClockAdvanced,
    CloseAdmission,
    QueueEntryRetired,
    RegisterAttempt,
    RunDrained,
    SlotChargeEnded,
    SlotReleased,
)
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
    RunInvocationCheckpointObserved,
    RunInvocationCheckpointRequested,
    RunSessionsDrainRequested,
    SessionDrainRequested,
    SessionInputReceived,
    SessionObserved,
    SessionsAcquireRequested,
    SteerReceived,
    TurnInputsReserved,
    TurnObserved,
    TurnRequested,
)
from .types.settlement import (
    AdoptionObserved,
    AssessmentSubmitted,
    AttemptSettled,
    OwnershipSettled,
    SettlementDependencyResolved,
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
            | AttemptReopenRequested()
            | SlotChargeEnded()
            | QueueEntryRetired()
            | RegisterAttempt()
            | AttemptReady()
            | SlotReleased()
            | ClockAdvanced()
            | AdmissionControl()
            | AdoptionFenceLifted()
            | AdmitAttempt()
            | CloseAdmission()
            | RunDrained()
        ):
            return Area.SCHEDULING
        case (
            AttemptEvaluationExhausted()
            | AttemptEvaluationHistoryUpdated()
            | RevisionOperationObserved()
            | AttemptRegistered()
            | AttemptReacquireRequested()
            | InitialSessionsReady()
            | InitialSessionsFailed()
            | InvocationChargeRequested()
            | AttemptSetupFailed()
            | InvocationEnded()
            | InvocationCheckpointRequested()
            | AttemptChargeRefundRequested()
            | ScopeReopenRequested()
            | ScopeReopenAdmitted()
            | ReacquisitionReady()
            | ScopeAdmissionReopened()
            | ReleaseDependencyObserved()
            | ReleaseDependencyBlocked()
            | RevisionOperationRequested()
            | AttemptAdmitted()
            | WorkspaceObserved()
            | RetireRequested()
            | InvocationCheckpointed()
            | RetentionRequired()
        ):
            return Area.ATTEMPTS
        case (
            RunInvocationCheckpointObserved()
            | RunInvocationCheckpointRequested()
            | RunSessionsDrainRequested()
            | RegisteredTurnRequested()
            | SessionsAcquireRequested()
            | InvocationChargesAuthorized()
            | InvocationCancellationRequested()
            | TurnInputsReserved()
            | SessionDrainRequested()
            | InvocationCheckpointAvailable()
            | SessionInputReceived()
            | InputReservationRequested()
            | InputAcceptanceObserved()
            | InputReservationReleased()
            | InvocationChargeRefunded()
            | TurnRequested()
            | TurnObserved()
            | SessionObserved()
            | SteerReceived()
            | InterruptRequested()
        ):
            return Area.SESSIONS
        case (
            RegisteredJobObserved()
            | JobTerminationRequested()
            | JobsDrainRequested()
            | MeasurementSubmissionObserved()
            | ContinuationJobsChanged()
            | ContinuationRetireRequested()
            | ContinuationReopenRequested()
            | ContinuationScopeReopened()
            | RegisteredJobRequested()
            | MeasurementRequested()
            | JobObserved()
            | TurnSuspended()
            | DeadlineReached()
        ):
            return Area.EVALUATION
        case (
            SettlementDependencyResolved()
            | AssessmentSubmitted()
            | OwnershipSettled()
            | WinnerProposed()
            | AdoptionObserved()
            | AttemptSettled()
        ):
            return Area.SETTLEMENT
        case (
            DecisionDependencyResolved()
            | RequestPrepared()
            | DispatchAuthorized()
            | RequestObserved()
            | RecoveryStarted()
            | RecoveryReady()
            | ReconciliationDeadline()
            | OperationRetireRequested()
            | DecisionCompleted()
        ):
            return Area.INTENTS
        case DecisionSubmitted() | ProposalSubmitted() | RunControlEvent():
            raise ContractError(("event",), "kernel event requires translation")
        case _:
            assert_never(event)
