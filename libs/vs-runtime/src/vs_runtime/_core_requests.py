"""Closed request routing and typed execution boundaries for the core shell.

These runtime roles translate core requests into owning-library calls. They do
not duplicate the agent/evaluation library contracts. B/C/D2 supply translators;
unbound roles return an explicit refusal without fabricating lifecycle facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import (
    AdoptionRequest,
    AdoptRevision,
    BlockIntent,
    CancelOwnedJob,
    CancelOwnedResource,
    CancelTurn,
    CloseAttemptScope,
    CloseSession,
    CollectEvidence,
    CoreEvent,
    DiscardWorkspace,
    DispatchTurn,
    EnsureSession,
    EnsureWorkspace,
    EvaluationRequest,
    ExecuteRegisteredOperation,
    HostFence,
    InspectOwnedJob,
    InspectRequest,
    InspectTurn,
    ObserveOwnedJob,
    Request,
    RequestBase,
    RequestId,
    RequestObserved,
    RestoreRevision,
    ResumeSessionTurn,
    RetainRevision,
    SessionRequest,
    SnapshotAndRetain,
    SnapshotAndRetainRun,
    SubmitMeasurement,
    VerifyAdoption,
    WorkspaceRequest,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


class ExecutorRole(StrEnum):
    """Closed runtime translators, with their implementation lane."""

    WORKSPACES = "workspaces"
    SESSIONS = "sessions"
    EVALUATION = "evaluation"
    OPERATIONS = "operations"
    SEMANTIC_EVENTS = "semantic_events"


class ExecutionContext(BaseModel):
    """Current host authorization, distinct from stable request/payload identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    fence: HostFence
    now_at: float = Field(ge=0, allow_inf_nan=False)
    payload_digest: str = Field(min_length=1)


class ExecutionResult(BaseModel):
    """Observations enqueue atomically; executors never mutate core state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    observation: RequestObserved
    owner_events: tuple[CoreEvent, ...] = ()


class ExecutorRefusal(BaseModel):
    """Named missing implementation, carrying no acceptance or release proof."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    request_id: RequestId
    role: ExecutorRole
    detail: str


type ExecutionOutcome = ExecutionResult | ExecutorRefusal


class WorkspaceRequests(Protocol):
    """D2 translates workspace, checkpoint and adoption requests."""

    async def execute(
        self,
        request: WorkspaceRequest | AdoptionRequest | SnapshotAndRetainRun,
        context: ExecutionContext,
    ) -> ExecutionOutcome: ...


class SessionRequests(Protocol):
    """B translates canonical session requests through vs-agent interfaces."""

    async def execute(
        self, request: SessionRequest, context: ExecutionContext
    ) -> ExecutionOutcome: ...


class EvaluationRequests(Protocol):
    """C translates jobs, evidence and scope closure through vs-evaluation."""

    async def execute(
        self, request: EvaluationRequest | CloseAttemptScope, context: ExecutionContext
    ) -> ExecutionOutcome: ...


class OperationExecutor(Protocol):
    """Closed registered-operation catalog and exact target inspection/cancel."""

    async def execute(
        self,
        request: ExecuteRegisteredOperation | InspectRequest | CancelOwnedResource,
        context: ExecutionContext,
    ) -> ExecutionOutcome: ...


class SemanticEvents(Protocol):
    """Committed diagnostic publication, deduplicated by canonical request ID."""

    async def execute(
        self, request: BlockIntent, context: ExecutionContext
    ) -> ExecutionOutcome: ...


REQUEST_DISPATCH: Mapping[type[RequestBase], ExecutorRole] = MappingProxyType(
    {
        EnsureWorkspace: ExecutorRole.WORKSPACES,
        RestoreRevision: ExecutorRole.WORKSPACES,
        SnapshotAndRetain: ExecutorRole.WORKSPACES,
        RetainRevision: ExecutorRole.WORKSPACES,
        DiscardWorkspace: ExecutorRole.WORKSPACES,
        CloseAttemptScope: ExecutorRole.EVALUATION,
        EnsureSession: ExecutorRole.SESSIONS,
        DispatchTurn: ExecutorRole.SESSIONS,
        InspectTurn: ExecutorRole.SESSIONS,
        CancelTurn: ExecutorRole.SESSIONS,
        CloseSession: ExecutorRole.SESSIONS,
        ResumeSessionTurn: ExecutorRole.SESSIONS,
        SnapshotAndRetainRun: ExecutorRole.WORKSPACES,
        SubmitMeasurement: ExecutorRole.EVALUATION,
        ObserveOwnedJob: ExecutorRole.EVALUATION,
        InspectOwnedJob: ExecutorRole.EVALUATION,
        CancelOwnedJob: ExecutorRole.EVALUATION,
        CollectEvidence: ExecutorRole.EVALUATION,
        AdoptRevision: ExecutorRole.WORKSPACES,
        VerifyAdoption: ExecutorRole.WORKSPACES,
        ExecuteRegisteredOperation: ExecutorRole.OPERATIONS,
        InspectRequest: ExecutorRole.OPERATIONS,
        CancelOwnedResource: ExecutorRole.OPERATIONS,
        BlockIntent: ExecutorRole.SEMANTIC_EVENTS,
    }
)


@dataclass(frozen=True)
class RefusingRequestExecution:
    """Explicit skeleton gap. No I/O and no synthetic success/Unknown event."""

    role: ExecutorRole

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutorRefusal:
        """Refuse at the role interface, preserving the durable identity."""
        del context
        if request.request_id is None:
            message = "request_id: execution requires a canonical identity"
            raise ValueError(message)
        return ExecutorRefusal(
            request_id=request.request_id,
            role=self.role,
            detail=f"{self.role.value} executor not bound; owning lane must supply implementation",
        )


@dataclass(frozen=True)
class RequestExecutors:
    """Wiring selects one translator per role; every request has an explicit route."""

    workspaces: WorkspaceRequests = field(
        default_factory=lambda: RefusingRequestExecution(ExecutorRole.WORKSPACES)
    )
    sessions: SessionRequests = field(
        default_factory=lambda: RefusingRequestExecution(ExecutorRole.SESSIONS)
    )
    evaluation: EvaluationRequests = field(
        default_factory=lambda: RefusingRequestExecution(ExecutorRole.EVALUATION)
    )
    operations: OperationExecutor = field(
        default_factory=lambda: RefusingRequestExecution(ExecutorRole.OPERATIONS)
    )
    semantic_events: SemanticEvents = field(
        default_factory=lambda: RefusingRequestExecution(ExecutorRole.SEMANTIC_EVENTS)
    )

    async def dispatch(self, request: Request, context: ExecutionContext) -> ExecutionOutcome:
        """Dispatch by exact closed variant, with statically narrowed role input."""
        if isinstance(
            request,
            CloseAttemptScope
            | SubmitMeasurement
            | ObserveOwnedJob
            | InspectOwnedJob
            | CancelOwnedJob
            | CollectEvidence,
        ):
            return await self.evaluation.execute(request, context)
        if isinstance(
            request,
            EnsureWorkspace
            | RestoreRevision
            | SnapshotAndRetain
            | RetainRevision
            | DiscardWorkspace
            | SnapshotAndRetainRun
            | AdoptRevision
            | VerifyAdoption,
        ):
            return await self.workspaces.execute(request, context)
        if isinstance(
            request,
            EnsureSession
            | DispatchTurn
            | InspectTurn
            | CancelTurn
            | CloseSession
            | ResumeSessionTurn,
        ):
            return await self.sessions.execute(request, context)
        if isinstance(request, ExecuteRegisteredOperation | InspectRequest | CancelOwnedResource):
            return await self.operations.execute(request, context)
        return await self.semantic_events.execute(request, context)
