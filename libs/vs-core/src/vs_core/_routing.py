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
from .types.kernel import (
    CoreEvent,
    DecisionCompleted,
    DecisionSubmitted,
    ProposalSubmitted,
    RunControlEvent,
    Signal,
)
from .types.records import (
    BlockedRecordStalled,
    DispatchAuthorized,
    OperationRetireRequested,
    RequestObserved,
    RequestPrepared,
    RequestUndispatchable,
    RetryDue,
    TurnHeld,
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
    ParallelLimitChanged,
    QueueEntryRetired,
    RegisterAttempt,
    RunDrained,
    SlotChargeEnded,
    SlotReleased,
)
from .types.sessions import (
    AcquisitionStalled,
    CheckpointStalled,
    InputAcceptanceObserved,
    InputMidTurnReleased,
    InputOfferedMidTurn,
    InputReservationReleased,
    InputReservationRequested,
    InputTakenMidTurn,
    InputWithdrawRequested,
    InterruptRequested,
    InvocationCancellationRequested,
    InvocationChargeRefunded,
    InvocationChargesAuthorized,
    InvocationCheckpointAvailable,
    InvocationCheckpointPending,
    RegisteredTurnRequested,
    RunInvocationCheckpointObserved,
    RunInvocationCheckpointRequested,
    RunSessionsDrainRequested,
    SessionDrainRequested,
    SessionInputReceived,
    SessionObserved,
    SessionsAcquireRequested,
    SessionWaitLapsed,
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
    WinnerProposed,
)

SIGNAL_ORDER = (
    Area.SCHEDULING,
    Area.ATTEMPTS,
    Area.SESSIONS,
    Area.EVALUATION,
    Area.SETTLEMENT,
    Area.RECORDS,
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
            | ParallelLimitChanged()
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
            | CheckpointStalled()
            | AcquisitionStalled()
            | SessionWaitLapsed()
            | InvocationCheckpointPending()
            | SessionInputReceived()
            | InputWithdrawRequested()
            | InputOfferedMidTurn()
            | InputTakenMidTurn()
            | InputMidTurnReleased()
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
            | RunJobsCancelRequested()
            | MeasurementSubmissionObserved()
            | MeasurementSubmissionEnded()
            | ContinuationJobsChanged()
            | ContinuationRetireRequested()
            | ContinuationReopenRequested()
            | ContinuationScopeReopened()
            | RegisteredJobRequested()
            | MeasurementRequested()
            | AgentMeasurementRequested()
            | JobObserved()
            | TurnSuspended()
            | DeadlineReached()
            | ObservationsDue()
            | JobWaitLapsed()
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
            | RetryDue()
            | RequestUndispatchable()
            | BlockedRecordStalled()
            | TurnHeld()
            | OperationRetireRequested()
            | DecisionCompleted()
        ):
            return Area.RECORDS
        case DecisionSubmitted() | ProposalSubmitted() | RunControlEvent():
            raise ContractError(("event",), "kernel event requires translation")
        case _:
            assert_never(event)
