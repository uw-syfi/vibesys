"""Aggregate contracts; no reducer implementation depends on another area."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import Field

from .attempts import AttemptsEvent, AttemptsState, AttemptView
from .common import (
    ArtifactRef,
    Capabilities,
    ControlInput,
    Count,
    EventCursor,
    HostFence,
    Limits,
    OperationDescriptor,
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
)
from .intents import IntentsEvent, IntentsState, OperationResult, OperationView, Request
from .scheduling import (
    AdmitAttempt,
    AttemptReady,
    CloseAdmission,
    RunDrained,
    SchedulingEvent,
    SchedulingState,
    SchedulingView,
)
from .sessions import SessionsEvent, SessionsState, SessionView, TurnResult
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

    decision: Decision
    payload_digest: str
    feedback: DecisionFeedback


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
    sessions: tuple[SessionView, ...]
    operations: tuple[OperationView, ...]
    measurements: tuple[EvidenceRef, ...]
    settlements: tuple[Settlement, ...]
    artifacts: tuple[ArtifactRef, ...]
    controls: tuple[ControlInput, ...]


class DecisionSubmitted(Value):
    """Decision submitted lifecycle contract."""

    kind: Literal["decision_submitted"] = "decision_submitted"
    decision: Decision
    expected_revision: RevisionNumber


class RunControlEvent(Value):
    """Run control event lifecycle contract."""

    kind: Literal["run_control"] = "run_control"
    control: ControlInput
    now_at: Seconds


class ControlChanged(Value):
    """Control changed lifecycle contract."""

    kind: Literal["control_changed"] = "control_changed"
    control: ControlInput


class RunEnded(Value):
    """Run ended lifecycle contract."""

    kind: Literal["run_ended"] = "run_ended"
    result: RunResultProposal


type CoreEvent = Annotated[
    DecisionSubmitted
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
    | MeasurementResult
    | ResumeAuthorized
    | OperationResult
    | ControlChanged
    | AdoptionResult
    | RunEnded,
    Field(discriminator="kind"),
]
# All propagation uses typed area events, plus neutral admission/drain signals.
type Signal = Annotated[
    SchedulingEvent
    | AttemptsEvent
    | SessionsEvent
    | EvaluationEvent
    | SettlementEvent
    | IntentsEvent
    | AdmitAttempt
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
    """Read-only cross-area facts at the latest propagated state."""

    run: RunState
    scheduling: SchedulingState
    attempts: AttemptsState
    sessions: SessionsState
    evaluation: EvaluationState
    settlement: SettlementState
    intents: IntentsState
    registry: tuple[OperationDescriptor, ...]


class SchedulingContext(AreaContext):
    """Scheduling reads ownership and authoritative run limits."""


class AttemptsContext(AreaContext):
    """Attempts reads release, session and settlement facts."""


class SessionsContext(AreaContext):
    """Sessions reads workspace and continuation authority."""


class EvaluationContext(AreaContext):
    """Evaluation reads invocation, ownership and run deadlines."""


class SettlementContext(AreaContext):
    """Settlement reads evidence, ownership and retention facts."""


class IntentsContext(AreaContext):
    """Intents reads scope generations and registry descriptors."""


class Transition(Value):
    """Transition lifecycle contract."""

    state: CoreState
    requests: tuple[Request, ...] = ()
    events: tuple[StrategyEvent, ...] = ()


class RunEnvelope[S: StrategyState](Value):
    """Run envelope lifecycle contract."""

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
