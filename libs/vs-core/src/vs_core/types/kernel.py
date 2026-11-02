"""Aggregate contracts; no reducer implementation depends on another area."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .attempts import (
    AttemptExhausted,
    AttemptsEvent,
    AttemptsState,
    AttemptView,
    CheckpointResolved,
)
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
    RunPark,
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
    JobQueued,
    MeasurementResult,
    ResumeAuthorized,
    ResumeRefused,
    TurnSuspended,
)
from .measurement import ComparisonSettled
from .records import (
    OperationResult,
    OperationView,
    Outbound,
    RecordsEvent,
    RecordsState,
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
    InterruptFailed,
    SessionProjection,
    SessionsEvent,
    SessionsState,
    TurnResult,
    YieldPublicationFailed,
)
from .settlement import (
    AdoptionFailed,
    AdoptionResult,
    AdoptionView,
    AttemptSettled,
    EvidenceRequirements,
    RunResultProposal,
    Settlement,
    SettlementEvent,
    SettlementState,
)
from .steps import StepsState
from .strategy import (
    Command,
    DecisionFeedback,
    SearchDeclaration,
    StrategyState,
    Submission,
    Wait,
)
from .until import Failed, Until


class DecisionReceipt(Value):
    """Command receipt lifecycle contract."""

    decision_id: DecisionId
    decision: Command | None = None
    until: Until | None = None
    """What core stored for the decision's ``Within``; None for a ``Finish``, which has none."""
    limit: Seconds | None = None
    """The decision's ``Within`` as a duration, which its requests count from their dispatch."""
    payload_digest: str
    feedback: DecisionFeedback
    request_ids: tuple[RequestId, ...] = ()
    completion: CompletionStatus | None = None


class RunState(Value):
    """Run state lifecycle contract."""

    run_id: RunId
    generation: Count = 0
    status: RunStatus = RunStatus.RUNNING
    park: RunPark | None = None
    now_at: Seconds
    deadline_at: Seconds
    duration_seconds: Seconds | None = Field(default=None, gt=0)
    """Original epoch duration; older records use the supplied run configuration once."""
    facts: RunFacts
    capabilities: Capabilities = Capabilities()
    limits: Limits
    requirements: EvidenceRequirements = EvidenceRequirements()
    declaration: SearchDeclaration
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


NO_STEPS = StepsState()
"""The steps of a log with none: ``CoreState`` leaves them out of its JSON, so identities hold."""


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
    records: RecordsState = RecordsState()
    steps: StepsState = Field(
        default=NO_STEPS, exclude_if=lambda value: value is NO_STEPS or value == NO_STEPS
    )
    """The attempts the log records, which only the recovery rule's fold writes."""


class CoreView(Value):
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
    adoption: AdoptionView | None = None
    # Earliest core time a ClockAdvanced does work: a paced job poll or a request's retry comes
    # due. None when none is scheduled. A step's limit is not one: `decide` ends it, and the
    # runner loop wakes at it.
    next_observe_at: Seconds | None = None


class DecisionSubmitted(Value):
    """Command submitted lifecycle contract."""

    kind: Literal["decision_submitted"] = "decision_submitted"
    decision: Submission
    expected_revision: RevisionNumber

    @property
    def command(self) -> Command:
        """The request or finish the decision asks core to act on."""
        return self.decision.request if isinstance(self.decision, Wait) else self.decision


class ProposalSubmitted(Value):
    """Ordered decisions observe one revision and commit with one revision increment."""

    kind: Literal["proposal_submitted"] = "proposal_submitted"
    decisions: tuple[Submission, ...]
    expected_revision: RevisionNumber


class DecisionCompleted(Value):
    """Leaves acknowledge semantic completion after all required lifecycle work."""

    kind: Literal["decision_completed"] = "decision_completed"
    decision_id: DecisionId
    status: CompletionStatus


_PAUSES = ("pause", "pause_now", "park")


class RunControlEvent(Value):
    """Operator control with explicit stop publication, never an invented result.

    A stop either carries its typed proposal, which the kernel turns into a draining ``Finish``
    (and preserves through draining; replay of one control identity with another proposal is a
    conflict), or carries none. A stop without a result only records the request and tells the
    search: it changes no status, and the search ends the run itself. Other actions cannot
    supply a result.

    A pause may carry ``drain_seconds``, how long its drain waits for running requests before it
    interrupts them; core records the deadline it makes in the run's park (``RunPark.deadline_at``).
    A Pause-now that names none waits the run's cleanup bound, and a pause that names none waits
    for its turns' own limits.
    """

    kind: Literal["run_control"] = "run_control"
    control: ControlInput
    now_at: Seconds
    result: RunResultProposal | None = None
    drain_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None

    @model_validator(mode="after")
    def stop_result_correspondence(self) -> RunControlEvent:
        """Only an explicit stop proposal may authorize final run publication by the kernel."""
        if self.result is not None and self.control.action != "stop":
            raise ContractValidationError("result", "allowed only for stop control")
        if self.drain_seconds is not None and self.control.action not in _PAUSES:
            raise ContractValidationError("drain_seconds", "allowed only for a pause")
        return self


class ControlChanged(Value):
    """Control changed lifecycle contract."""

    kind: Literal["control_changed"] = "control_changed"
    control: ControlInput


class StopLiftRecorded(Value):
    """An explicit resume lifted the run's standing stop (the control that did it).

    Core publishes it after the ``ControlChanged`` of the same control, so a consumer sees
    the stop and then its lift. It is recorded only while the stop stands and the run has not
    committed its ending (`vs_core._run_pause.control_refusal`).
    """

    kind: Literal["stop_lift_recorded"] = "stop_lift_recorded"
    control: ControlInput
    new_epoch: bool = Field(default=False, exclude_if=lambda value: not value)


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
    | RecordsEvent
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
    | CheckpointResolved
    | ComparisonSettled
    | InterruptCompleted
    | MeasurementResult
    | JobQueued
    | ResumeAuthorized
    | ResumeRefused
    | TurnSuspended
    | YieldPublicationFailed
    | InterruptFailed
    | OperationResult
    | Failed
    | ControlChanged
    | StopLiftRecorded
    | AdoptionResult
    | AdoptionFailed
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
    | RecordsEvent
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
    requests: tuple[Outbound, ...] = ()
    events: tuple[StrategyEvent, ...] = ()


class AreaContext(Value):
    """Base for strict immutable, area-specific cross-area projections."""


class SchedulingContext(AreaContext):
    """Required scheduling cross-area facts, including run session ownership."""

    run: RunState
    attempts: AttemptsState
    records: RecordsState
    sessions: SessionsState
    settlement: SettlementState
    evaluation: EvaluationState


class AttemptsContext(AreaContext):
    """Required attempts cross-area facts."""

    run: RunState
    scheduling: SchedulingState
    sessions: SessionsState
    evaluation: EvaluationState
    settlement: SettlementState
    records: RecordsState


class SessionsContext(AreaContext):
    """Required sessions cross-area facts."""

    run: RunState
    attempts: AttemptsState
    evaluation: EvaluationState
    records: RecordsState


class EvaluationContext(AreaContext):
    """Required evaluation cross-area facts."""

    run: RunState
    attempts: AttemptsState
    sessions: SessionsState
    records: RecordsState


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
    records: RecordsState


class RecordsContext(AreaContext):
    """Required records cross-area facts."""

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

    ``schema_version`` is ``ENVELOPE_SCHEMA_VERSION`` when written; the shape is pinned by
    vs-core's ``tests/envelope.persisted-schema.txt``, and a change every saved envelope
    survives keeps the version (``scripts/check_persisted_schema_compat.py``). An envelope of
    an older supported version is migrated to the current one before it is decoded
    (``vs_core._envelope_migrations``, checked by ``tests/test_envelope_migrations.py``); one
    of any other version is refused, before its shape is validated, with an error that names
    the version and says whether an older or a newer VibeSys wrote it. One of the current
    version holding a field or value this build does not know is refused as written by a
    newer VibeSys (`vs_core._registry.decode_stored`).
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
