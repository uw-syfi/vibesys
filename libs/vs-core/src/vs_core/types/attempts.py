"""Workspace ownership, generations, checkpoint and charge contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from .common import (
    AttemptId,
    AttemptRef,
    ChargeReceipt,
    Count,
    Generation,
    ItemId,
    Observation,
    ReleaseDependency,
    RequestBase,
    RequestId,
    RevisionRef,
    SessionId,
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
    ACTIVE = "active"
    CLOSING = "closing"
    TERMINAL = "terminal"
    PARKED = "parked"
    BLOCKED = "blocked"


class AttemptView(Value):
    """Attempt view lifecycle contract."""

    attempt_id: AttemptId
    item_id: ItemId
    generation: Generation
    phase: AttemptPhase
    workspace: WorkspacePlan
    budget: AttemptBudget
    checkpoint: RevisionRef | None = None
    charges: tuple[ChargeReceipt, ...] = ()
    parent: AttemptRef | None = None
    pending_intents: tuple[RequestId, ...] = ()
    sessions: tuple[SessionId, ...] = ()
    release_dependencies: tuple[ReleaseDependency, ...] = ()


class AttemptsState(Value):
    """Attempts state lifecycle contract."""

    attempts: tuple[AttemptView, ...] = ()


class AttemptAdmitted(Value):
    """Attempt admitted lifecycle contract."""

    kind: Literal["attempt_admitted"] = "attempt_admitted"
    request: AttemptRequest
    workspace: WorkspacePlan
    budget: AttemptBudget
    initial_sessions: tuple[SessionSpec, ...] = ()


class WorkspaceObserved(Value):
    """Workspace observed lifecycle contract."""

    kind: Literal["workspace_observed"] = "workspace_observed"
    attempt: AttemptRef
    observation: Observation
    revision: RevisionRef | None = None


class RetireRequested(Value):
    """Retire requested lifecycle contract."""

    kind: Literal["retire_requested"] = "retire_requested"
    attempt: AttemptRef
    disposition: Literal["park", "cancel", "settle"]


class InvocationCheckpointed(Value):
    """Invocation checkpointed lifecycle contract."""

    kind: Literal["invocation_checkpointed"] = "invocation_checkpointed"
    attempt: AttemptRef
    revision: RevisionRef
    charge: ChargeReceipt


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


type AttemptsEvent = Annotated[
    AttemptAdmitted
    | WorkspaceObserved
    | RetireRequested
    | InvocationCheckpointed
    | RetentionRequired,
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
