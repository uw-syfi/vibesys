"""Workspace ownership, generations, checkpoint and charge contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from .common import (
    AttemptId,
    AttemptRef,
    ChargeId,
    ChargeReceipt,
    ContinuationId,
    Count,
    DecisionId,
    ExecuteRegisteredOperation,
    Generation,
    InvocationRef,
    ItemId,
    Observation,
    OperationId,
    ReleaseDependency,
    RequestBase,
    RequestId,
    RevisionAuthority,
    RevisionRef,
    ScopeReopenNormalization,
    Seconds,
    SessionId,
    SetupFailureKind,
    Value,
    WorkspaceMode,
)
from .scheduling import AttemptRequest
from .sessions import SessionSpec


class WorkspacePlan(Value):
    """Workspace plan lifecycle contract."""

    mode: WorkspaceMode
    base: RevisionRef
    parked_predecessor: AttemptRef | None = None


class AttemptBudget(Value):
    """Attempt budget lifecycle contract."""

    admission_charge: Count = 1
    paid_invocation_limit: Count = 1
    retry_limit: Count = 0
    refund_limit: Count = 0


class AttemptPhase(StrEnum):
    """Attempt phase lifecycle contract."""

    QUEUED = "queued"
    ACQUIRING = "acquiring"
    ACTIVE = "active"
    CLOSING = "closing"
    TERMINAL = "terminal"
    PARKED = "parked"
    BLOCKED = "blocked"


class AttemptClosure(Value):
    """First committed retirement path; late completion cannot replace it.

    Closure fences mutation and ends occupancy charging. Final settlement and
    physical slot release require every dependency to finish conclusively.
    """

    disposition: Literal["park", "cancel", "settle"]
    requested_at: Seconds
    authority: RequestId
    admission_id: DecisionId


class AttemptCheckpoint(Value):
    """Retained revision attributed to its exact request and optional invocation."""

    invocation: InvocationRef | None
    request_id: RequestId
    revision: RevisionRef
    retention: Literal["wip", "candidate"]


class AttemptView(Value):
    """Attempt authority, receipt history, retained checkpoints and release graph.

    Accepted queued starts already have an ADMISSION receipt. Slot admission
    transitions QUEUED to ACQUIRING without a second charge. Initial workspace
    and sessions must all be ready before execution. Reopen has a fresh episode
    but no additional ADMISSION charge; historical checkpoints retain attribution.
    """

    attempt_id: AttemptId
    item_id: ItemId
    generation: Generation
    phase: AttemptPhase
    workspace: WorkspacePlan
    budget: AttemptBudget
    admission_id: DecisionId | None = None
    closure: AttemptClosure | None = None
    checkpoints: tuple[AttemptCheckpoint, ...] = ()
    charges: tuple[ChargeReceipt, ...] = ()
    parent: AttemptRef | None = None
    pending_intents: tuple[RequestId, ...] = ()
    sessions: tuple[SessionId, ...] = ()
    release_dependencies: tuple[ReleaseDependency, ...] = ()

    @property
    def checkpoint(self) -> RevisionRef | None:
        """Project latest retained checkpoint without losing historical attribution."""
        return self.checkpoints[-1].revision if self.checkpoints else None


class AttemptsState(Value):
    """Attempts state lifecycle contract."""

    attempts: tuple[AttemptView, ...] = ()


class AttemptAdmitted(Value):
    """Attempt admitted lifecycle contract."""

    kind: Literal["attempt_admitted"] = "attempt_admitted"
    admission_id: DecisionId
    request: AttemptRequest
    workspace: WorkspacePlan
    budget: AttemptBudget
    initial_sessions: tuple[SessionSpec, ...] = ()


class RevisionOperationRequested(Value):
    """Snapshot and retention extensions pass through workspace authority."""

    kind: Literal["revision_operation_requested"] = "revision_operation_requested"
    request: ExecuteRegisteredOperation
    authority: RevisionAuthority


class RevisionOperationObserved(Value):
    """Typed revision acknowledgements remain under attempts authority."""

    kind: Literal["revision_operation_observed"] = "revision_operation_observed"
    operation_id: OperationId
    observation: Observation
    revision: RevisionRef | None = None


class WorkspaceObserved(Value):
    """Workspace observed lifecycle contract."""

    kind: Literal["workspace_observed"] = "workspace_observed"
    attempt: AttemptRef
    observation: Observation
    revision: RevisionRef | None = None


class RetireRequested(Value):
    """Durable retirement authority for the exact current occupancy episode.

    Settlement-first rejects withdrawal. Withdrawal-first retains its cleanup
    disposition through late completion and stop. Drain writers before retention
    or discard; unresolved dependencies block final settlement and slot release.
    """

    kind: Literal["retire_requested"] = "retire_requested"
    attempt: AttemptRef
    disposition: Literal["park", "cancel", "settle"]
    authority: RequestId
    admission_id: DecisionId
    requested_at: Seconds


class InvocationCheckpointed(Value):
    """Exact retained revision proof for one invocation and checkpoint request.

    charge must match a recorded receipt; this observation cannot mint accounting
    authority. Keep prior checkpoints for assessment of earlier invocations.
    """

    kind: Literal["invocation_checkpointed"] = "invocation_checkpointed"
    attempt: AttemptRef
    revision: RevisionRef
    charge: ChargeReceipt
    invocation: InvocationRef
    checkpoint_request: RequestId


class RetentionRequired(Value):
    """Retention required lifecycle contract."""

    kind: Literal["retention_required"] = "retention_required"
    attempt: AttemptRef
    retention: Literal["discard", "wip", "candidate"]
    revision: RevisionRef | None = None


class EnsureWorkspace(RequestBase):
    """Ensure workspace lifecycle contract."""

    kind: Literal["ensure_workspace"] = "ensure_workspace"
    attempt: AttemptRef
    plan: WorkspacePlan


class RestoreRevision(RequestBase):
    """Restore revision lifecycle contract."""

    kind: Literal["restore_revision"] = "restore_revision"
    attempt: AttemptRef
    revision: RevisionRef


class SnapshotAndRetain(RequestBase):
    """Snapshot and retain lifecycle contract."""

    kind: Literal["snapshot_and_retain"] = "snapshot_and_retain"
    attempt: AttemptRef
    retention: Literal["wip", "candidate"]


class RetainRevision(RequestBase):
    """Retain revision lifecycle contract."""

    kind: Literal["retain_revision"] = "retain_revision"
    attempt: AttemptRef
    revision: RevisionRef
    retention: Literal["wip", "candidate"]


class DiscardWorkspace(RequestBase):
    """Discard workspace lifecycle contract."""

    kind: Literal["discard_workspace"] = "discard_workspace"
    attempt: AttemptRef


class CloseAttemptScope(RequestBase):
    """Close attempt scope lifecycle contract."""

    kind: Literal["close_attempt_scope"] = "close_attempt_scope"
    attempt: AttemptRef


class AttemptRegistered(Value):
    """Record queued ownership and ADMISSION receipt before acquiring capacity."""

    kind: Literal["attempt_registered"] = "attempt_registered"
    request: AttemptRequest
    workspace: WorkspacePlan
    budget: AttemptBudget
    initial_sessions: tuple[SessionSpec, ...] = ()


class ScopeReopenRequested(Value):
    """Evaluation forwards validated guarded reopening to retirement authority."""

    kind: Literal["scope_reopen_requested"] = "scope_reopen_requested"
    request: ExecuteRegisteredOperation
    normalization: ScopeReopenNormalization


class ScopeReopenAdmitted(Value):
    """New capacity episode has been acquired before lease reacquisition."""

    kind: Literal["scope_reopen_admitted"] = "scope_reopen_admitted"
    attempt: AttemptRef
    request_id: RequestId
    admission_id: DecisionId


class AttemptReacquireRequested(Value):
    """Acquire retained workspace and sessions for an admitted reopen."""

    kind: Literal["attempt_reacquire_requested"] = "attempt_reacquire_requested"
    attempt: AttemptRef
    continuation_id: ContinuationId
    request_id: RequestId
    admission_id: DecisionId
    base: RevisionRef


class ReacquisitionReady(Value):
    """Positive acquisition proof for the matching reopening episode."""

    kind: Literal["reacquisition_ready"] = "reacquisition_ready"
    attempt: AttemptRef
    request_id: RequestId
    admission_id: DecisionId


class ScopeAdmissionReopened(Value):
    """External scope admission observation; unknown retains capacity and fences."""

    kind: Literal["scope_admission_reopened"] = "scope_admission_reopened"
    attempt: AttemptRef
    continuation_id: ContinuationId
    park_authority: RequestId
    operation_id: OperationId
    observation: Observation
    admission: Literal["reopened", "closed", "unknown"]


class InitialSessionsReady(Value):
    """All required initial sessions acquired for the matching attempt episode."""

    kind: Literal["initial_sessions_ready"] = "initial_sessions_ready"
    attempt: AttemptRef
    admission_id: DecisionId
    session_ids: tuple[SessionId, ...]


class InitialSessionsFailed(Value):
    """One acquisition group member failed; late acceptance joins cleanup."""

    kind: Literal["initial_sessions_failed"] = "initial_sessions_failed"
    attempt: AttemptRef
    admission_id: DecisionId
    session_id: SessionId
    observation: Observation
    failure: SetupFailureKind


class InvocationChargeRequested(Value):
    """Request attempt-scoped accounting authority for one admitted invocation."""

    kind: Literal["invocation_charge_requested"] = "invocation_charge_requested"
    attempt: AttemptRef
    invocation: InvocationRef


class AttemptSetupFailed(Value):
    """Failed setup charges once per cycle, not once per failing group member."""

    kind: Literal["attempt_setup_failed"] = "attempt_setup_failed"
    attempt: AttemptRef
    observation: Observation
    failure: SetupFailureKind


class InvocationEnded(Value):
    """Exact terminal invocation facts passed to attempt accounting."""

    kind: Literal["invocation_ended"] = "invocation_ended"
    attempt: AttemptRef
    invocation: InvocationRef
    observation: Observation


class InvocationCheckpointRequested(Value):
    """Request retained checkpoint only after writer termination proof."""

    kind: Literal["invocation_checkpoint_requested"] = "invocation_checkpoint_requested"
    attempt: AttemptRef
    invocation: InvocationRef
    retention: Literal["wip", "candidate"]
    authority: RequestId


class AttemptChargeRefundRequested(Value):
    """Refund bounded recorded charge using exact interruption or unsupported proof."""

    kind: Literal["attempt_charge_refund_requested"] = "attempt_charge_refund_requested"
    attempt: AttemptRef
    charge_id: ChargeId
    amount: Count
    reason: Literal["interrupted", "unsupported"]
    authority: RequestId
    checkpoint_authority: RequestId | None = None


class ReleaseDependencyObserved(Value):
    """Exact release graph edge proof; idle or cancellation alone cannot release."""

    kind: Literal["release_dependency_observed"] = "release_dependency_observed"
    attempt: AttemptRef
    dependency: ReleaseDependency
    observation: Observation


class ReleaseDependencyBlocked(Value):
    """Unresolved release ownership blocks final settlement and slot release."""

    kind: Literal["release_dependency_blocked"] = "release_dependency_blocked"
    attempt: AttemptRef
    dependency: ReleaseDependency
    authority: RequestId
    diagnostic: str


class AttemptExhausted(Value):
    """Budget exhaustion feedback grants no release or automatic retry authority."""

    kind: Literal["attempt_exhausted"] = "attempt_exhausted"
    attempt: AttemptRef
    reason: Literal["paid-limit", "retry-limit", "refund-limit"]


type AttemptsEvent = Annotated[
    AttemptAdmitted
    | RevisionOperationObserved
    | RevisionOperationRequested
    | WorkspaceObserved
    | RetireRequested
    | InvocationCheckpointed
    | RetentionRequired
    | AttemptRegistered
    | ScopeReopenRequested
    | ScopeReopenAdmitted
    | AttemptReacquireRequested
    | ReacquisitionReady
    | ScopeAdmissionReopened
    | InitialSessionsReady
    | InitialSessionsFailed
    | InvocationChargeRequested
    | AttemptSetupFailed
    | InvocationEnded
    | InvocationCheckpointRequested
    | AttemptChargeRefundRequested
    | ReleaseDependencyObserved
    | ReleaseDependencyBlocked,
    Field(discriminator="kind"),
]
type WorkspaceRequest = Annotated[
    EnsureWorkspace
    | RestoreRevision
    | SnapshotAndRetain
    | RetainRevision
    | DiscardWorkspace
    | CloseAttemptScope,
    Field(discriminator="kind"),
]
