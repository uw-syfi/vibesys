"""Session lifetime, invocation acceptance and continuation fencing."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, assert_never

from pydantic import ConfigDict, Field, model_validator

from .common import (
    ArtifactRef,
    AttemptRef,
    ChargeId,
    ChargeReceipt,
    CheckpointDecline,
    ContinuationId,
    ContractValidationError,
    Count,
    DecisionId,
    ExecuteRegisteredOperation,
    Generation,
    InputId,
    InterruptPolicy,
    InvocationId,
    InvocationRef,
    Observation,
    ObservationStatus,
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
    TurnFailureKind,
    Value,
    WorkspaceRef,
    validate_setup_failure,
)
from .evaluation import Continuation, ResumeAuthorizationReceipt
from .evaluation_history import EvaluationHistoryCursor
from .session_inputs import InputRecord, InvocationInputTarget, SessionInput
from .until import ReplyError, Until
from .waits import WaitOn


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
    model: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    """The model the operator chose for the role this run; None leaves the run's default."""


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
    charge_class: Literal["paid", "correction", "resume", "free"]
    max_turns: int = Field(default=1, ge=1)
    predecessor: InvocationRef | None = None
    on_interrupt: InterruptPolicy = Field(
        default=InterruptPolicy.RERUN, exclude_if=lambda policy: policy is InterruptPolicy.RERUN
    )
    """What an interrupted attempt of this turn means: only the asker knows if a person watches."""


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


def session_may_wait(phase: SessionPhase) -> bool:
    """Whether a session in this phase may wait: a waiting one, or an unknown one inspected.

    A session whose acquisition or turn core cannot settle is unknown; while the inspection
    that settles it is out, it waits on that inspection. One with nothing out waits on nothing
    core can name, and carries no wait.
    """
    return session_waits(phase) or phase == SessionPhase.UNKNOWN


def turn_acquires(invocation: Invocation) -> bool:
    """Whether a turn is still acquiring: requested, charged or not, with no result yet."""
    return invocation.phase == SessionPhase.ACQUIRING and invocation.observation is None


def turn_may_wait(invocation: Invocation) -> bool:
    """Whether a turn may carry a wait: while it acquires, or while its yield is unpublished."""
    return turn_acquires(invocation) or invocation.pending_suspension is not None


def session_waits(phase: SessionPhase) -> bool:
    """Whether a session in this phase waits on a request's result."""
    match phase:
        case SessionPhase.ACQUIRING | SessionPhase.EXECUTING | SessionPhase.CLOSING:
            return True
        case (
            SessionPhase.IDLE
            | SessionPhase.CHECKPOINTED
            | SessionPhase.SUSPENDED
            | SessionPhase.TERMINAL
            | SessionPhase.UNKNOWN
        ):
            return False
        case _:
            assert_never(phase)


class SessionView(Value):
    """Persisted session identity, lease and invocation-acceptance facts.

    InputRecord owns reservation state. SessionProjection derives its outward
    reserved_inputs field, while this persisted model stores no duplicate input
    authority. Idle is not session release proof.
    """

    # Fields declared with this class hold exactly this class (a subclass instance is rebuilt as
    # one on validation), so one remembered JSON text per instance holds wherever it sits.
    model_config = ConfigDict(revalidate_instances="subclass-instances")

    spec: SessionSpec
    scope: Scope
    generation: Generation
    phase: SessionPhase
    invocation: InvocationId | None = None
    accepted: bool = False
    resource_id: ResourceId | None = None
    pending_records: tuple[RequestId, ...] = ()
    acceptance_sequence: int | None = Field(default=None, ge=0)
    continuation_id: ContinuationId | None = None
    wait: WaitOn | None = None
    """The requests the session waits on while acquiring, executing or closing: the one
    that acquires, runs or closes it, or the inspection that stands in for one."""

    @model_validator(mode="after")
    def waits_only_while_waiting(self) -> SessionView:
        """A session that waits on nothing carries no wait.

        The other half, that a waiting session always carries one, holds at the end of every
        step (``unmet_waits``): a transition builds the session before it names the request.
        """
        if self.wait is not None and not session_may_wait(self.phase):
            raise ContractValidationError(
                "wait", f"a session in phase {self.phase.value} waits on nothing"
            )
        return self


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
    Failed retention does not publish it: core ends the turn with
    ``YieldPublicationFailed`` and clears it. Duplicates cannot replace or restore it.
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
    guidance_ids: tuple[InputId, ...] = ()
    """The standing inputs the turn was dispatched with, in the order it received them."""
    evaluation_prefix: EvaluationHistoryCursor | None = None
    pending_suspension: Continuation | None = None
    until: Until
    """When core ends this turn, from the ``Within`` its decision was accepted with."""
    limit: Seconds | None = None
    """How long the turn may run once the shell dispatches it: what ``until`` allowed, as a
    duration. The turn's own requests count it from their dispatch, not from this admission.
    Admission always sets it; None is a turn whose ``until`` is already absolute."""
    wait: WaitOn | None = None
    """What the turn waits on. While it acquires: its session, whose acquisition carries it
    on. While its yield is unpublished: the checkpoint request that retains its workspace, or
    the inspection of the turn that lets core request one."""

    @model_validator(mode="after")
    def waits_only_while_waiting(self) -> Invocation:
        """Only a turn acquiring or yielding waits; the other half holds after each step."""
        if self.wait is not None and not turn_may_wait(self):
            raise ContractValidationError(
                "wait",
                f"a turn in phase {self.phase.value} acquires nothing and publishes no yield",
            )
        return self

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
    wait: WaitOn | None
    """What a draining claim waits on once its turn ended: the checkpoint request that
    retains the interrupted turn's workspace."""

    @model_validator(mode="after")
    def waits_only_while_draining(self) -> InterruptClaim:
        """Only a draining claim waits; the other half holds after each step."""
        if self.wait is not None and self.phase != "draining":
            raise ContractValidationError("wait", f"a {self.phase} interruption waits on nothing")
        return self


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
    until: Until


class RegisteredTurnRequested(Value):
    """Custom turns share session acceptance, charging and cleanup authority."""

    kind: Literal["registered_turn_requested"] = "registered_turn_requested"
    request: ExecuteRegisteredOperation
    turn: TurnSpec
    until: Until


class TurnObserved(Value):
    """Turn observed lifecycle contract."""

    kind: Literal["turn_observed"] = "turn_observed"
    suspension: Continuation | None = None
    invocation: InvocationRef
    observation: Observation
    output_schema: SchemaRef | None = None
    output_json: str | None = None
    reply_errors: tuple[ReplyError, ...] = ()
    """The executor's schema check of a reply it rejected, by field."""


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


TURN_FAILURE_DETAIL_LIMIT = 4000
"""Longest failure text a TurnResult carries, in characters."""


class TurnResult(Value):
    """Terminal result of one turn, with the failure text when it did not succeed.

    ``failure`` is None exactly when the turn succeeded; ``detail`` is the executor's
    diagnostic, cut to ``TURN_FAILURE_DETAIL_LIMIT`` characters, so a correction
    prompt can name the error without reading the raw observation.
    """

    kind: Literal["turn_result"] = "turn_result"
    invocation: InvocationRef
    observation: Observation
    output_schema: SchemaRef | None = None
    output_json: str | None = None
    failure: TurnFailureKind | None = None
    detail: str = Field(default="", max_length=TURN_FAILURE_DETAIL_LIMIT)
    reply_errors: tuple[ReplyError, ...] = ()
    """The executor's schema check of a reply it rejected, by field; empty for any other end."""
    suspending: bool = False
    """Core recorded the turn's wait, so a ``TurnSuspended`` follows when it checkpoints.

    If the checkpoint is declined or fails, ``YieldPublicationFailed`` follows instead.

    False for a turn that ends without one, whatever its reply says: the agent never
    asked to wait, core refused the wait it asked for, or the host lost the wait. A
    strategy must not wait for a resume of a turn that is not suspending.
    """

    @model_validator(mode="after")
    def failure_matches_status(self) -> TurnResult:
        """A failure kind and its text belong to a turn that did not succeed."""
        if self.observation.status == ObservationStatus.SUCCEEDED and (
            self.failure is not None or self.detail or self.reply_errors
        ):
            raise ContractValidationError("failure", "a succeeded turn carries no failure")
        return self


class CheckpointFailure(StrEnum):
    """Why a wait that hangs on a checkpoint ended without it, as a closed set."""

    CHECKPOINT_DECLINED = "checkpoint_declined"
    """The checkpoint was declined or failed; ``decline`` says why."""

    NO_CHECKPOINT = "no_checkpoint"
    """No checkpoint was requested or is still coming, so the wait could never end."""

    UNPUBLISHABLE = "unpublishable"
    """A checkpoint came back but core could not act on it: the scope stopped accepting
    work, or the checkpoint no longer matched the turn."""

    REFUND_UNAPPLIED = "refund_unapplied"
    """An interruption retained its checkpoint, but the attempt would not apply its refund
    (the charge, a limit or the attempt's state no longer allowed it)."""


def _failure_detail(reason: CheckpointFailure, decline: CheckpointDecline | None) -> str:
    return reason.value if decline is None else f"{reason.value} ({decline.value})"


def _decline_names_the_declined_checkpoint(reason: CheckpointFailure, decline: object) -> None:
    if (reason == CheckpointFailure.CHECKPOINT_DECLINED) != (decline is not None):
        raise ContractValidationError("decline", "required exactly for checkpoint_declined")


class YieldPublicationFailed(Value):
    """A turn core recorded as suspending was ended: its suspension will not be published.

    ``TurnResult.suspending`` promised a ``TurnSuspended``. This event is the other way that
    promise ends. The turn is terminal and failed, its session accepts a new turn, and its
    scope no longer holds a suspension claim. A strategy handles it like a failed turn:
    ask again, settle the attempt as failed, or abandon the workstream. Core emits exactly
    one of the two for every suspending turn.
    """

    kind: Literal["yield_publication_failed"] = "yield_publication_failed"
    invocation: InvocationRef
    reason: CheckpointFailure
    decline: CheckpointDecline | None = None

    @model_validator(mode="after")
    def decline_matches_reason(self) -> YieldPublicationFailed:
        """A decline reason accompanies exactly the declined-checkpoint failure."""
        _decline_names_the_declined_checkpoint(self.reason, self.decline)
        return self

    @property
    def detail(self) -> str:
        """The reason in one line, for a strategy's failure record."""
        return _failure_detail(self.reason, self.decline)


class InterruptFailed(Value):
    """An interruption core accepted cannot complete: its checkpoint will not come.

    The other way an accepted interruption ends besides ``InterruptCompleted``. The turn is
    cancelled but nothing was retained, no refund was paid, and the claim is closed, so a
    strategy may propose a replacement turn knowing the workspace was not checkpointed.
    """

    kind: Literal["interrupt_failed"] = "interrupt_failed"
    invocation: InvocationRef
    reason: CheckpointFailure
    decline: CheckpointDecline | None = None

    @model_validator(mode="after")
    def decline_matches_reason(self) -> InterruptFailed:
        """A decline reason accompanies exactly the declined-checkpoint failure."""
        _decline_names_the_declined_checkpoint(self.reason, self.decline)
        return self

    @property
    def detail(self) -> str:
        """The reason in one line, for a strategy's failure record."""
        return _failure_detail(self.reason, self.decline)


class CheckpointStalled(Value):
    """Internal signal: a wait on this invocation's checkpoint has nothing left to end it.

    Raised by the step when it reaches quiescence with a pending suspension or a draining
    interruption claim and no checkpoint request live or in flight; the sessions reducer
    ends each wait with ``YieldPublicationFailed`` or ``InterruptFailed``. A stall naming
    an invocation whose waits still have a producer, or that has none, changes nothing.
    """

    kind: Literal["checkpoint_stalled"] = "checkpoint_stalled"
    invocation: InvocationRef


class AcquisitionStalled(Value):
    """Internal signal: a turn is acquiring and nothing is left that could carry it forward.

    Raised by the step when it reaches quiescence with an invocation still acquiring (its
    charge or input reservation was never granted, or its session lease was lost) and no
    request about its session live or in flight. The sessions reducer ends the turn with a
    cancelled ``TurnResult`` whose detail names the stall, and completes its decision cancelled.
    A stall naming an invocation that still has a producer changes nothing.
    """

    kind: Literal["acquisition_stalled"] = "acquisition_stalled"
    invocation: InvocationRef


class InvocationCheckpointPending(Value):
    """Internal signal: the checkpoint of a terminal turn is requested as *request_id*.

    The attempt that owns the turn raises it in the step it sends the checkpoint request, or
    when the request it was asked for is already out. A turn whose yield is unpublished, and a
    draining interruption of it, then name that request as what they wait on. A checkpoint the
    attempt declines raises nothing, so the waits it would have carried have nothing to end
    them and the step ends them by name (``CheckpointStalled``).
    """

    kind: Literal["invocation_checkpoint_pending"] = "invocation_checkpoint_pending"
    invocation: InvocationRef
    request_id: RequestId


class SessionWaitLapsed(Value):
    """A request a waiting session names ended without moving the session.

    Internal signal raised once a step's other signals settle, never shell input. The
    session's own transition takes each answer of its requests, so a session still naming an
    ended request is one the answer did not move: the request blocked at its bound, or the
    session refused what it said. The session waits again on the requests it names that are
    still open, or, with none left, no longer knows what the executor holds (``UNKNOWN``).
    """

    kind: Literal["session_wait_lapsed"] = "session_wait_lapsed"
    session_id: SessionId
    request_id: RequestId


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
    guidance: tuple[SessionInput, ...] = ()
    """The standing inputs in force for this turn when it was dispatched; none is reserved."""


class InspectTurn(RequestBase):
    """Inspect turn lifecycle contract."""

    kind: Literal["inspect_turn"] = "inspect_turn"
    invocation: InvocationRef
    dispatch: RequestId | None = None
    """The request that dispatched the turn, when core can name it.

    An executor attributes its facts about a turn that never started to this request,
    because it has no record of its own to name the dispatch by. Without it such an
    executor can only report the turn as unknown.
    """


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
    resource_id: ResourceId | None = None
    """The physical lease this request releases, as the session recorded it.

    None means no lease was ever recorded (an acquisition that never completed).
    An executor releases only this lease, never a newer one under the same
    ``session_id``.
    """
    episode: DecisionId | None = None
    """The admission episode the released lease was held under (None for run scope)."""


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
    guidance: tuple[SessionInput, ...] = ()
    """The standing inputs in force for this turn when it was dispatched; none is reserved."""
    continuation_id: ContinuationId
    publication: ResumeAuthorizationReceipt | None = None
    """The publication proof core held for this continuation when it issued the resume.

    An executor compares it with its own continuation record (successor, evidence
    and history cursor) before it dispatches. None only for requests issued before
    this field existed.
    """


class SessionInputReceived(Value):
    """Receive one durable input occurrence before reservation."""

    kind: Literal["session_input_received"] = "session_input_received"
    input: SessionInput


class InputWithdrawRequested(Value):
    """End one standing input: the signal an operator's withdraw control raises.

    Only core raises it, from a recorded withdraw control; it is not accepted as ingress.
    """

    kind: Literal["input_withdraw_requested"] = "input_withdraw_requested"
    input_id: InputId


class InputOfferedMidTurn(Value):
    """The shell is about to offer an interrupt-mode input to a running turn.

    Committed before the provider is asked, so a crash after the call cannot lose the
    fact that it may have been made. Core records the offer only for a pending
    interrupt-mode input and an executing session turn that would take it; any other
    offer is a no-op the shell sees in the committed state before it calls.
    """

    kind: Literal["input_offered_mid_turn"] = "input_offered_mid_turn"
    input_id: InputId
    invocation: InvocationRef
    now_at: Seconds


class InputTakenMidTurn(Value):
    """The provider took an offered input into its running turn (it answered ``DELIVERED``).

    Committed after the provider's answer. Only a taken offer joins the turn when the turn's
    acceptance commits; an offer the turn ends without having taken waits for the next turn.
    A crash between the answer and this commit leaves the offer untaken, so the input reaches
    a turn again: delivery is at least once. A take for an offer core no longer holds for that
    turn changes nothing.
    """

    kind: Literal["input_taken_mid_turn"] = "input_taken_mid_turn"
    input_id: InputId
    invocation: InvocationRef
    now_at: Seconds


class InputMidTurnReleased(Value):
    """An offered input did not stay in the running turn: it waits for the next turn instead.

    The shell raises it when the provider takes no mid-turn input or has no turn running, or
    when it finds an offer an earlier host made. The turn's own result raises it, before the
    turn's acceptance, for a message the provider took and then refused (``refused``): the
    agent never read that one. An offer that already settled, or that core holds for another
    turn, changes nothing.
    """

    kind: Literal["input_mid_turn_released"] = "input_mid_turn_released"
    input_id: InputId
    invocation: InvocationRef
    now_at: Seconds
    refused: bool = False
    """The provider took the message and then refused it, so the agent did not read it."""


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
    disposition: Literal["cancel", "settle"]


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
    The shell persists record before I/O and replays the same request identity.
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
    | InputWithdrawRequested
    | InputOfferedMidTurn
    | InputTakenMidTurn
    | InputMidTurnReleased
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
    | RunInvocationCheckpointObserved
    | CheckpointStalled
    | AcquisitionStalled
    | SessionWaitLapsed
    | InvocationCheckpointPending,
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
