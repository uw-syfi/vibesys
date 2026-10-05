"""Aggregate contracts; no reducer implementation depends on another area."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import Field, model_validator

from .attempts import AttemptExhausted, AttemptsEvent, AttemptsState, AttemptView
from .common import (
    ArtifactRef,
    Capabilities,
    CompletionStatus,
    ContractValidationError,
    ControlInput,
    Count,
    DecisionId,
    EventCursor,
    HostFence,
    Limits,
    OperationDescriptor,
    RequestId,
    RevisionNumber,
    RunFacts,
    RunId,
    RunStatus,
    SchemaRef,
    Seconds,
    StrategyId,
    Value,
)
from .evaluation import (
    EvaluationEvent,
    EvaluationState,
    EvidenceRef,
    MeasurementResult,
    ResumeAuthorized,
    TurnSuspended,
)
from .intents import (
    IntentBlocked,
    IntentsEvent,
    IntentsState,
    OperationResult,
    OperationView,
    Request,
)
from .scheduling import (
    AdmitAttempt,
    AttemptReady,
    CloseAdmission,
    RegisterAttempt,
    RunDrained,
    SchedulingEvent,
    SchedulingState,
    SchedulingView,
)
from .session_inputs import InputDelivered, InputDropped, InputRecord
from .sessions import (
    InterruptCompleted,
    SessionProjection,
    SessionsEvent,
    SessionsState,
    TurnResult,
)
from .settlement import (
    AdoptionResult,
    AttemptSettled,
    EvidenceRequirements,
    RunResultProposal,
    Settlement,
    SettlementEvent,
    SettlementState,
)
from .strategy import Decision, DecisionFeedback, Proposal, StrategyDeclaration, StrategyState


class DecisionReceipt(Value):
    """Decision receipt lifecycle contract."""

    decision_id: DecisionId
    decision: Decision | None = None
    payload_digest: str
    feedback: DecisionFeedback
    request_ids: tuple[RequestId, ...] = ()
    completion: CompletionStatus | None = None


class RunState(Value):
    """Run state lifecycle contract."""

    run_id: RunId
    generation: Count = 0
    status: RunStatus = RunStatus.RUNNING
    now_at: Seconds
    deadline_at: Seconds
    facts: RunFacts
    capabilities: Capabilities = Capabilities()
    limits: Limits = Limits()
    requirements: EvidenceRequirements = EvidenceRequirements()
    declaration: StrategyDeclaration
    controls: tuple[ControlInput, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    receipts: tuple[DecisionReceipt, ...] = ()
    result: RunResultProposal | None = None


class RunSummary(Value):
    """Run summary lifecycle contract."""

    run_id: RunId
    generation: Count
    status: RunStatus
    now_at: Seconds
    deadline_at: Seconds
    result: RunResultProposal | None


class CoreState(Value):
    """Core state lifecycle contract."""

    revision: RevisionNumber = 0
    run: RunState
    registry: tuple[OperationDescriptor, ...] = ()
    scheduling: SchedulingState = SchedulingState()
    attempts: AttemptsState = AttemptsState()
    sessions: SessionsState = SessionsState()
    evaluation: EvaluationState = EvaluationState()
    settlement: SettlementState = SettlementState()
    intents: IntentsState = IntentsState()


class RunView(Value):
    """Run view lifecycle contract."""

    revision: RevisionNumber
    run: RunSummary
    facts: RunFacts
    capabilities: Capabilities
    limits: Limits
    scheduling: SchedulingView
    attempts: tuple[AttemptView, ...]
    sessions: tuple[SessionProjection, ...]
    operations: tuple[OperationView, ...]
    measurements: tuple[EvidenceRef, ...]
    settlements: tuple[Settlement, ...]
    artifacts: tuple[ArtifactRef, ...]
    controls: tuple[ControlInput, ...]
    inputs: tuple[InputRecord, ...]
    # Earliest core time a paced job poll comes due, None when none is scheduled.
    next_observe_at: Seconds | None = None


class DecisionSubmitted(Value):
    """Decision submitted lifecycle contract."""

    kind: Literal["decision_submitted"] = "decision_submitted"
    decision: Decision
    expected_revision: RevisionNumber


class ProposalSubmitted(Value):
    """Ordered decisions observe one revision and commit with one revision increment."""

    kind: Literal["proposal_submitted"] = "proposal_submitted"
    decisions: tuple[Decision, ...]
    expected_revision: RevisionNumber


class DecisionCompleted(Value):
    """Leaves acknowledge semantic completion after all required lifecycle work."""

    kind: Literal["decision_completed"] = "decision_completed"
    decision_id: DecisionId
    status: CompletionStatus


class RunControlEvent(Value):
    """Operator control with explicit stop publication, never an invented result.

    Stop requires its typed proposal. Other actions cannot supply one. The kernel
    records the first committed stop result and preserves it through draining;
    replay of one control identity with another proposal is a conflict.
    """

    kind: Literal["run_control"] = "run_control"
    control: ControlInput
    now_at: Seconds
    result: RunResultProposal | None = None

    @model_validator(mode="after")
    def stop_result_correspondence(self) -> RunControlEvent:
        """Only an explicit stop proposal may authorize final run publication."""
        if (self.control.action == "stop") != (self.result is not None):
            raise ContractValidationError("result", "required exactly for stop control")
        return self


class ControlChanged(Value):
    """Control changed lifecycle contract."""

    kind: Literal["control_changed"] = "control_changed"
    control: ControlInput


class RunEnded(Value):
    """Final publication after ownership drains and input receipts settle.

    Every remaining input occurrence receives its terminal disposal receipt
    before this event is published. Unknown ownership or unsettled input
    finalization prevents irreversible run completion.
    """

    kind: Literal["run_ended"] = "run_ended"
    result: RunResultProposal


type CoreEvent = Annotated[
    DecisionSubmitted
    | ProposalSubmitted
    | SchedulingEvent
    | AttemptsEvent
    | SessionsEvent
    | EvaluationEvent
    | SettlementEvent
    | IntentsEvent
    | RunControlEvent,
    Field(discriminator="kind"),
]
type StrategyEvent = Annotated[
    DecisionFeedback
    | AttemptReady
    | AttemptSettled
    | TurnResult
    | InputDelivered
    | InputDropped
    | AttemptExhausted
    | InterruptCompleted
    | MeasurementResult
    | ResumeAuthorized
    | TurnSuspended
    | OperationResult
    | IntentBlocked
    | ControlChanged
    | AdoptionResult
    | RunEnded,
    Field(discriminator="kind"),
]
# All propagation uses typed area events, plus neutral admission/drain signals.
type Signal = Annotated[
    DecisionCompleted
    | SchedulingEvent
    | AttemptsEvent
    | SessionsEvent
    | EvaluationEvent
    | SettlementEvent
    | IntentsEvent
    | AdmitAttempt
    | RegisterAttempt
    | CloseAdmission
    | RunDrained,
    Field(discriminator="kind"),
]


class AreaChange[S: Value](Value):
    """Area change lifecycle contract."""

    state: S
    signals: tuple[Signal, ...] = ()
    requests: tuple[Request, ...] = ()
    events: tuple[StrategyEvent, ...] = ()


class AreaContext(Value):
    """Base for strict immutable, area-specific cross-area projections."""


class SchedulingContext(AreaContext):
    """Required scheduling cross-area facts, including run session ownership."""

    run: RunState
    attempts: AttemptsState
    intents: IntentsState
    sessions: SessionsState


class AttemptsContext(AreaContext):
    """Required attempts cross-area facts."""

    run: RunState
    scheduling: SchedulingState
    sessions: SessionsState
    evaluation: EvaluationState
    settlement: SettlementState
    intents: IntentsState


class SessionsContext(AreaContext):
    """Required sessions cross-area facts."""

    run: RunState
    attempts: AttemptsState
    evaluation: EvaluationState
    intents: IntentsState


class EvaluationContext(AreaContext):
    """Required evaluation cross-area facts."""

    run: RunState
    attempts: AttemptsState
    sessions: SessionsState
    intents: IntentsState


class SettlementContext(AreaContext):
    """Required settlement facts, including the run's declared registry authority.

    Decoding an operation with an available codec does not prove that the run
    declared it. Registered judge eligibility requires the exact descriptor.
    """

    registry: tuple[OperationDescriptor, ...]
    run: RunState
    attempts: AttemptsState
    sessions: SessionsState
    evaluation: EvaluationState
    intents: IntentsState


class IntentsContext(AreaContext):
    """Required intents cross-area facts."""

    run: RunState
    registry: tuple[OperationDescriptor, ...]
    attempts: AttemptsState
    sessions: SessionsState
    evaluation: EvaluationState


class Transition(Value):
    """Transition lifecycle contract."""

    state: CoreState
    requests: tuple[Request, ...] = ()
    events: tuple[StrategyEvent, ...] = ()


class RunEnvelope[S: StrategyState](Value):
    """Atomic core, strategy, fence and cursor envelope with explicit versioning.

    Version 3 adds explicit history, source receipts and run-owned lifecycle
    authority. Version 2 requires the selected v2_to_v3_migration(registry). Earlier
    envelopes require an
    explicitly selected pure migration before nested models are decoded; new
    defaults never stand in for missing historical ownership proof.
    """

    schema_version: int = Field(ge=1)
    fence: HostFence
    strategy_id: StrategyId
    state_schema: SchemaRef
    core: CoreState
    strategy: S
    event_cursor: EventCursor

    @property
    def revision(self) -> RevisionNumber:
        """Core owns revision; the envelope only projects it."""
        return self.core.revision


class Strategy[S: StrategyState](Protocol):
    """Strategy lifecycle contract."""

    @property
    def state(self) -> S:
        """State lifecycle contract."""
        ...

    @property
    def declaration(self) -> StrategyDeclaration:
        """Declaration lifecycle contract."""
        ...

    def bind(self, state: S) -> Strategy[S]:
        """Bind lifecycle contract."""
        ...

    def decide(self, view: RunView) -> Proposal[S]:
        """Decide lifecycle contract."""
        ...

    def on_event(self, view: RunView, event: StrategyEvent) -> S:
        """On_event lifecycle contract."""
        ...
