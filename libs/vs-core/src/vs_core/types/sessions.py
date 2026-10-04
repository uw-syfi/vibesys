"""Session lifetime, invocation acceptance and continuation fencing."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import (
    ArtifactRef,
    AttemptRef,
    ChargeId,
    ChargeReceipt,
    ContinuationId,
    ContractValidationError,
    Count,
    DecisionId,
    ExecuteRegisteredOperation,
    Generation,
    InputId,
    InvocationId,
    InvocationRef,
    Observation,
    OperationId,
    RequestBase,
    RequestId,
    ResourceId,
    RevisionRef,
    RoleId,
    SchemaRef,
    Scope,
    Seconds,
    SessionId,
    Value,
    WorkspaceRef,
)
from .evaluation import Continuation
from .session_inputs import InputRecord, InvocationInputTarget, SessionInput


class Access(StrEnum):
    """Access lifecycle contract."""

    READ_ONLY = "read-only"
    WRITE_ARTIFACTS = "write-artifacts"
    WRITE_CANDIDATE = "write-candidate"


class SessionSpec(Value):
    """Session identity, access and lifetime independent of scientific role policy.

    Reuse requires durable session/resource correspondence and positive
    reattachment. Reuse policy alone never permits replacing a lost conversation.
    """

    session_id: SessionId
    role_id: RoleId
    policy: Literal["fresh", "reuse"]
    lifetime: Literal["ephemeral", "owner"]
    access: Access


class TurnSpec(Value):
    """One logical outward invocation with an executor-specific max_turns bound.

    Every charge class consumes exactly one TURN; max_turns does not multiply
    currency. Only paid consumes ATTEMPT. Corrections have distinct IDs and
    bounded predecessor chains; resume is a separate continuation transition.
    """

    session: SessionSpec
    invocation_id: InvocationId
    continuation_id: ContinuationId | None = None
    workspace: WorkspaceRef | Scope
    prompts: tuple[ArtifactRef, ...]
    output_schema: SchemaRef
    tool_policy: ArtifactRef | None = None
    artifact_dependencies: tuple[ArtifactRef, ...] = ()
    deadline_at: Seconds
    charge_class: Literal["paid", "correction", "resume", "free"]
    max_turns: int = Field(default=1, ge=1)
    predecessor: InvocationRef | None = None


class SessionPhase(StrEnum):
    """Session phase lifecycle contract."""

    ACQUIRING = "acquiring"
    IDLE = "idle"
    EXECUTING = "executing"
    CHECKPOINTED = "checkpointed"
    SUSPENDED = "suspended"
    CLOSING = "closing"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


class SessionView(Value):
    """Persisted session identity, lease and invocation-acceptance facts.

    InputRecord owns reservation state. SessionProjection derives its outward
    reserved_inputs field, while this persisted model stores no duplicate input
    authority. Idle is not session release proof.
    """

    spec: SessionSpec
    scope: Scope
    generation: Generation
    phase: SessionPhase
    invocation: InvocationId | None = None
    accepted: bool = False
    resource_id: ResourceId | None = None
    pending_intents: tuple[RequestId, ...] = ()
    acceptance_sequence: int | None = Field(default=None, ge=0)
    continuation_id: ContinuationId | None = None


class SessionProjection(SessionView):
    """Read-only session view with artifacts derived from active input reservations.

    This model is projection output only. SessionsState stores SessionView and
    InputRecord remains the sole reservation authority. reserved_inputs follows
    sequence then stable input identity, omitting delivered and dropped receipts.
    """

    reserved_inputs: tuple[ArtifactRef, ...]


class Invocation(Value):
    """Durable invocation history required for assessment and callback finality.

    input_ids is the dispatched occurrence manifest. reserved_inputs preserves
    immutable artifact payload history only; InputRecord owns reservations and
    terminal delivery/drop receipts. Replay cannot reserve or deliver twice.
    """

    invocation: InvocationRef
    scope: Scope
    turn: TurnSpec
    registered_operation: OperationId | None = None
    phase: SessionPhase
    observation: Observation | None = None
    output_schema: SchemaRef | None = None
    output_json: str | None = None
    reserved_inputs: tuple[ArtifactRef, ...] = ()
    input_ids: tuple[InputId, ...] = ()


class InterruptClaim(Value):
    """Durable interruption sequence, independent of input acceptance.

    Cancellation must prove terminality, then retention must prove the checkpoint,
    before bounded refund and completion. Replacement is a separate proposed
    invocation. Unknown acceptance triggers inspection and retains reservations.
    """

    invocation: InvocationRef
    authority: RequestId
    refund: Count
    phase: Literal["pending", "draining", "checkpointed", "completed", "blocked"] = "pending"
    checkpoint_authority: RequestId | None = None
    refunded_charge: ChargeId | None = None


class SessionAcquisitionGroup(Value):
    """All-or-cleanup initial session acquisition for one occupancy episode.

    A failed group never becomes ready after late acceptance; late leases join
    cleanup. Workspace and every required session must be ready before feedback.
    """

    attempt: AttemptRef
    admission_id: DecisionId
    scope: Scope
    session_ids: tuple[SessionId, ...]
    phase: Literal["acquiring", "ready", "failed"] = "acquiring"
    failure_request: RequestId | None = None


class SessionsState(Value):
    """Sessions state lifecycle contract."""

    sessions: tuple[SessionView, ...] = ()
    invocations: tuple[Invocation, ...] = ()
    inputs: tuple[InputRecord, ...] = ()
    run_charges: tuple[ChargeReceipt, ...] = ()
    interrupts: tuple[InterruptClaim, ...] = ()
    acquisition_groups: tuple[SessionAcquisitionGroup, ...] = ()

    @model_validator(mode="after")
    def distinct_input_occurrences(self) -> SessionsState:
        """Occurrence identity, rather than content digest, indexes durable input."""
        ids = tuple(record.input.input_id for record in self.inputs)
        if len(set(ids)) != len(ids):
            raise ContractValidationError("inputs", "duplicate input ID")
        return self


class TurnRequested(Value):
    """Turn requested lifecycle contract."""

    kind: Literal["turn_requested"] = "turn_requested"
    scope: Scope
    turn: TurnSpec


class RegisteredTurnRequested(Value):
    """Custom turns share session acceptance, charging and cleanup authority."""

    kind: Literal["registered_turn_requested"] = "registered_turn_requested"
    request: ExecuteRegisteredOperation
    turn: TurnSpec


class TurnObserved(Value):
    """Turn observed lifecycle contract."""

    kind: Literal["turn_observed"] = "turn_observed"
    suspension: Continuation | None = None
    invocation: InvocationRef
    observation: Observation
    output_schema: SchemaRef | None = None
    output_json: str | None = None


class SessionObserved(Value):
    """Session observed lifecycle contract."""

    kind: Literal["session_observed"] = "session_observed"
    session_id: SessionId
    observation: Observation


class SteerReceived(Value):
    """Steer received lifecycle contract."""

    kind: Literal["steer_received"] = "steer_received"
    invocation: InvocationRef
    inputs: tuple[SessionInput, ...]

    @model_validator(mode="after")
    def exact_targets(self) -> SteerReceived:
        """Reject invocation-specific inputs naming another invocation."""
        for item in self.inputs:
            if isinstance(item.target, InvocationInputTarget) and (
                item.target.invocation != self.invocation
            ):
                raise ContractValidationError("inputs.target.invocation", "differs from steer")
        ids = tuple(item.input_id for item in self.inputs)
        if len(set(ids)) != len(ids):
            raise ContractValidationError("inputs", "duplicate input ID")
        return self


class InterruptRequested(Value):
    """Interrupt requested lifecycle contract."""

    kind: Literal["interrupt_requested"] = "interrupt_requested"
    invocation: InvocationRef
    refund: Count = 0
    authority: RequestId


class TurnResult(Value):
    """Turn result lifecycle contract."""

    kind: Literal["turn_result"] = "turn_result"
    invocation: InvocationRef
    observation: Observation
    output_schema: SchemaRef | None = None
    output_json: str | None = None


class EnsureSession(RequestBase):
    """Acquire a lease or positively reattach required_resource.

    A requested existing identity cannot be substituted with a new conversation.
    Missing correspondence blocks reacquisition; unknown acceptance is inspected.
    """

    kind: Literal["ensure_session"] = "ensure_session"
    spec: SessionSpec
    required_resource: ResourceId | None = None


class DispatchTurn(RequestBase):
    """Dispatch turn lifecycle contract."""

    kind: Literal["dispatch_turn"] = "dispatch_turn"
    turn: TurnSpec
    inputs: tuple[SessionInput, ...] = ()


class InspectTurn(RequestBase):
    """Inspect turn lifecycle contract."""

    kind: Literal["inspect_turn"] = "inspect_turn"
    invocation: InvocationRef


class CancelTurn(RequestBase):
    """Cancel turn lifecycle contract."""

    kind: Literal["cancel_turn"] = "cancel_turn"
    invocation: InvocationRef


class CloseSession(RequestBase):
    """Close session lifecycle contract."""

    kind: Literal["close_session"] = "close_session"
    session_id: SessionId


class ResumeSessionTurn(RequestBase):
    """Resume session turn lifecycle contract."""

    kind: Literal["resume_session_turn"] = "resume_session_turn"
    turn: TurnSpec
    inputs: tuple[SessionInput, ...] = ()
    continuation_id: ContinuationId


class SessionInputReceived(Value):
    """Receive one durable input occurrence before reservation."""

    kind: Literal["session_input_received"] = "session_input_received"
    input: SessionInput


class InputReservationRequested(Value):
    """Reserve eligible pending inputs before dispatching the exact invocation."""

    kind: Literal["input_reservation_requested"] = "input_reservation_requested"
    invocation: InvocationRef


class TurnInputsReserved(Value):
    """Immutable occurrence manifest for the invocation dispatch payload."""

    kind: Literal["turn_inputs_reserved"] = "turn_inputs_reserved"
    invocation: InvocationRef
    input_ids: tuple[InputId, ...]


class InputAcceptanceObserved(Value):
    """Confirm delivery only for the exact accepted reserved invocation."""

    kind: Literal["input_acceptance_observed"] = "input_acceptance_observed"
    invocation: InvocationRef
    observation: Observation


class InputReservationReleased(Value):
    """Positive nonacceptance releases eligible reservations without retargeting."""

    kind: Literal["input_reservation_released"] = "input_reservation_released"
    invocation: InvocationRef
    observation: Observation


class SessionsAcquireRequested(Value):
    """Acquire every declared initial session for the exact occupancy episode."""

    kind: Literal["sessions_acquire_requested"] = "sessions_acquire_requested"
    attempt: AttemptRef
    admission_id: DecisionId
    scope: Scope
    specs: tuple[SessionSpec, ...]


class InvocationChargesAuthorized(Value):
    """Recorded charge identities authorize one outward invocation."""

    kind: Literal["invocation_charges_authorized"] = "invocation_charges_authorized"
    invocation: InvocationRef
    charge_ids: tuple[ChargeId, ...]


class InvocationCancellationRequested(Value):
    """Declared interruption requests cancellation with durable authority."""

    kind: Literal["invocation_cancellation_requested"] = "invocation_cancellation_requested"
    invocation: InvocationRef
    authority: RequestId


class InvocationCheckpointAvailable(Value):
    """Exact retained checkpoint permits interruption progress and safe successor."""

    kind: Literal["invocation_checkpoint_available"] = "invocation_checkpoint_available"
    invocation: InvocationRef
    request_id: RequestId
    revision: RevisionRef
    retention: Literal["wip", "candidate"]


class InvocationChargeRefunded(Value):
    """Exact bounded refund authority completes interruption accounting."""

    kind: Literal["invocation_charge_refunded"] = "invocation_charge_refunded"
    invocation: InvocationRef
    charge_id: ChargeId
    authority: RequestId


class InterruptCompleted(Value):
    """Checkpoint and bounded refund completed; replacement needs separate proposal."""

    kind: Literal["interrupt_completed"] = "interrupt_completed"
    invocation: InvocationRef
    checkpoint: RevisionRef
    refund: Count


class SessionDrainRequested(Value):
    """Drain invocation/session leases before retention or workspace release."""

    kind: Literal["session_drain_requested"] = "session_drain_requested"
    attempt: AttemptRef
    authority: RequestId
    disposition: Literal["park", "cancel", "settle"]


type SessionsEvent = Annotated[
    RegisteredTurnRequested
    | TurnRequested
    | TurnObserved
    | SessionObserved
    | SteerReceived
    | InterruptRequested
    | SessionInputReceived
    | InputReservationRequested
    | TurnInputsReserved
    | InputAcceptanceObserved
    | InputReservationReleased
    | SessionsAcquireRequested
    | InvocationChargesAuthorized
    | InvocationCancellationRequested
    | InvocationCheckpointAvailable
    | InvocationChargeRefunded
    | SessionDrainRequested,
    Field(discriminator="kind"),
]
type SessionRequest = Annotated[
    EnsureSession | DispatchTurn | InspectTurn | CancelTurn | CloseSession | ResumeSessionTurn,
    Field(discriminator="kind"),
]
