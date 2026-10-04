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
    RunId,
    SchemaRef,
    Scope,
    Seconds,
    SessionId,
    SetupFailureKind,
    Value,
    WorkspaceRef,
    validate_setup_failure,
)
from .evaluation import Continuation
from .evaluation_history import EvaluationHistoryCursor
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
    Run-owned invocations cannot consume ATTEMPT and therefore cannot be paid;
    they use free, correction or resume, each still consuming exactly one TURN.
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
    evaluation_prefix is the immutable preparation-history position of this
    attempt-paid cycle, captured by Sessions A with its initial ATTEMPT charge.
    Corrections and resumed successors inherit that exact prefix; a distinct
    paid cycle captures a new one. None means unavailable, never an empty prefix.
    pending_suspension retains the first canonical terminal yield while its
    checkpoint is pending. Sessions A persists it with the observation, then
    clears it atomically with checkpoint-backed TurnSuspended and completion.
    Failed retention preserves it; duplicates cannot replace or restore it.
    None means absent or consumed, never proof of checkpoint or publication.
    Acceptance, terminality and retained checkpoint proof remain leaf-owned.
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
    evaluation_prefix: EvaluationHistoryCursor | None = None
    pending_suspension: Continuation | None = None

    @model_validator(mode="after")
    def pending_suspension_identity(self) -> Invocation:
        """Pending yield names this exact invocation and a distinct same-session successor."""
        pending = self.pending_suspension
        if pending is None:
            return self
        if (
            pending.invocation != self.invocation
            or self.invocation.generation != self.scope.generation
            or self.turn.session.session_id != self.invocation.session_id
            or self.turn.invocation_id != self.invocation.invocation_id
        ):
            raise ContractValidationError(
                "pending_suspension", "invocation, scope or turn identity mismatch"
            )
        successor = pending.next_invocation
        if (
            successor.session_id != self.invocation.session_id
            or successor.generation != self.scope.generation
            or successor.invocation_id == self.invocation.invocation_id
        ):
            raise ContractValidationError(
                "pending_suspension.next_invocation", "requires distinct same-session successor"
            )
        return self

    @model_validator(mode="after")
    def attempt_evaluation_prefix(self) -> Invocation:
        """Run invocations have no attempt-paid history prefix authority."""
        if self.evaluation_prefix is not None and isinstance(self.scope.owner, RunId):
            raise ContractValidationError("evaluation_prefix", "requires attempt-paid ownership")
        if (
            self.evaluation_prefix is not None
            and self.invocation.generation != self.scope.generation
        ):
            raise ContractValidationError(
                "evaluation_prefix", "invocation generation differs from scope"
            )
        return self


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


class RunInvocationCheckpoint(Value):
    """Sessions A's immutable retained checkpoint proof for one run invocation.

    This receipt authorizes run-owned writable yield/resume only after the exact
    canonical snapshot request succeeds and writer termination is confirmed.
    Missing receipts never stand in for retained candidate or WIP authority.
    """

    invocation: InvocationRef
    scope: Scope
    request_id: RequestId
    revision: RevisionRef
    retention: Literal["wip", "candidate"]

    @model_validator(mode="after")
    def run_invocation_correspondence(self) -> RunInvocationCheckpoint:
        """Checkpoint authority belongs to the exact run invocation generation."""
        _validate_run_invocation(self.scope, self.invocation)
        return self


def _validate_run_invocation(scope: Scope, invocation: InvocationRef) -> None:
    """Reject attempt ownership and cross-generation run invocation authority."""
    if not isinstance(scope.owner, RunId):
        raise ContractValidationError("scope.owner", "requires run ownership")
    if scope.generation != invocation.generation:
        raise ContractValidationError("invocation.generation", "differs from scope")


class SessionsState(Value):
    """Sessions state lifecycle contract."""

    sessions: tuple[SessionView, ...] = ()
    invocations: tuple[Invocation, ...] = ()
    inputs: tuple[InputRecord, ...] = ()
    run_charges: tuple[ChargeReceipt, ...] = ()
    interrupts: tuple[InterruptClaim, ...] = ()
    acquisition_groups: tuple[SessionAcquisitionGroup, ...] = ()
    run_checkpoints: tuple[RunInvocationCheckpoint, ...] = ()

    @model_validator(mode="after")
    def distinct_run_checkpoints(self) -> SessionsState:
        """A checkpoint request and invocation/retention pair publish once."""
        requests = tuple(item.request_id for item in self.run_checkpoints)
        owners = tuple((item.invocation, item.retention) for item in self.run_checkpoints)
        if len(set(requests)) != len(requests) or len(set(owners)) != len(owners):
            raise ContractValidationError("run_checkpoints", "duplicate checkpoint authority")
        return self

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
    """Session setup observation with executor-owned typed failure classification.

    UNKNOWN requests reconciliation and grants no unsupported refund authority.
    Sessions A forwards the exact classification to InitialSessionsFailed;
    diagnostics and status cannot manufacture transient/permanent/unsupported.
    """

    kind: Literal["session_observed"] = "session_observed"
    session_id: SessionId
    observation: Observation
    failure: SetupFailureKind = SetupFailureKind.UNKNOWN

    @model_validator(mode="after")
    def failure_classification(self) -> SessionObserved:
        """Classification requires conclusive failure, separately from acceptance.

        An accepted-but-failed setup retains its lease cleanup obligations.
        Classification alone never proves nonacceptance or permits a refund.
        """
        validate_setup_failure(self.observation, self.failure)
        return self


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
    """Dispatch the exact turn authorized by an accepted canonical RequestTurn.

    Dispatch requires matching decision IDs, scope, TurnSpec and deadline, plus
    membership of this request ID in the canonical receipt. Resume turns also
    require exact continuation publication and applicable history/checkpoint
    proof before dispatch.
    """

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
    """Release the exact owned session lease without closing a reused session.

    An attempt-owned reusable session_id requires the current admission episode
    or durable proof identifying the exact old physical lease. A historical
    retirement receipt alone is insufficient. The kernel requires the current
    episode when only this request's reusable session identity is available.
    """

    kind: Literal["close_session"] = "close_session"
    session_id: SessionId


class ResumeSessionTurn(RequestBase):
    """Resume the exact turn authorized by an accepted canonical RequestTurn.

    Dispatch requires matching decision IDs, scope, TurnSpec and deadline, plus
    membership of this request ID in the canonical receipt. The resume also
    requires exact continuation publication and applicable history/checkpoint
    proof; missing historical authority never grants permission to dispatch.
    """

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


class RunSessionsDrainRequested(Value):
    """Accepted first Stop authority closes run-owned sessions before publication.

    Sessions A validates the canonical accepted Stop receipt, its run scope and
    result against the closing run. Sessions B finalizes inputs only after lease
    release. Idle, cancellation acknowledgement and missing resources do not
    prove physical drain. Attempts never own this run-wide finality authority.
    """

    kind: Literal["run_sessions_drain_requested"] = "run_sessions_drain_requested"
    scope: Scope
    authority: DecisionId

    @model_validator(mode="after")
    def run_scope(self) -> RunSessionsDrainRequested:
        """Attempt-scoped retirement cannot close run-owned conversations."""
        if not isinstance(self.scope.owner, RunId):
            raise ContractValidationError("scope.owner", "requires run ownership")
        return self


class RunInvocationCheckpointRequested(Value):
    """Run writer requests exact retained checkpoint after terminal turn proof.

    Sessions A owns preparation and receipt publication. Evaluation B consumes
    only the committed RunInvocationCheckpoint, never this request or None.
    """

    kind: Literal["run_invocation_checkpoint_requested"] = "run_invocation_checkpoint_requested"
    invocation: InvocationRef
    scope: Scope
    retention: Literal["wip", "candidate"]
    authority: RequestId

    @model_validator(mode="after")
    def run_invocation_correspondence(self) -> RunInvocationCheckpointRequested:
        """Run and invocation generations must agree before retention is proposed."""
        _validate_run_invocation(self.scope, self.invocation)
        return self


class SnapshotAndRetainRun(RequestBase):
    """Retain the run writer's exact workspace under a stable canonical request.

    Sessions A requires terminal invocation proof before preparation. Success is
    separately observed with a retained RevisionRef; missing revision is unknown.
    The shell persists intent before I/O and replays the same request identity.
    """

    kind: Literal["snapshot_and_retain_run"] = "snapshot_and_retain_run"
    invocation: InvocationRef
    retention: Literal["wip", "candidate"]

    @model_validator(mode="after")
    def run_invocation_correspondence(self) -> SnapshotAndRetainRun:
        """Reject attempts masquerading as invocation-owned run checkpoints."""
        _validate_run_invocation(self.scope, self.invocation)
        if self.admission_id is not None:
            raise ContractValidationError("admission_id", "run checkpoint has no admission")
        return self


class RunInvocationCheckpointObserved(Value):
    """Typed snapshot outcome, preserving request identity without implying success.

    Sessions A checks the canonical request, invocation and retained revision
    before publishing RunInvocationCheckpoint. None is absence of proof.
    """

    kind: Literal["run_invocation_checkpoint_observed"] = "run_invocation_checkpoint_observed"
    invocation: InvocationRef
    checkpoint_request: RequestId
    observation: Observation
    revision: RevisionRef | None = None

    @model_validator(mode="after")
    def exact_observation(self) -> RunInvocationCheckpointObserved:
        """Foreign snapshot acknowledgements cannot authorize retained run output."""
        _validate_run_invocation(self.observation.scope, self.invocation)
        if self.observation.request_id != self.checkpoint_request:
            raise ContractValidationError(
                "checkpoint_request", "differs from observation.request_id"
            )
        if self.observation.admission_id is not None:
            raise ContractValidationError(
                "observation.admission_id", "run checkpoint has no admission"
            )
        return self


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
    | SessionDrainRequested
    | RunSessionsDrainRequested
    | RunInvocationCheckpointRequested
    | RunInvocationCheckpointObserved,
    Field(discriminator="kind"),
]
type SessionRequest = Annotated[
    EnsureSession
    | DispatchTurn
    | InspectTurn
    | CancelTurn
    | CloseSession
    | ResumeSessionTurn
    | SnapshotAndRetainRun,
    Field(discriminator="kind"),
]
