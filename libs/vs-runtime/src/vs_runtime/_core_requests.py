"""Closed request routing and typed execution boundaries for the core shell.

These runtime roles translate core requests into owning-library calls. They do
not duplicate the agent/evaluation library contracts. B/C/D2 supply translators;
unbound roles return an explicit refusal without fabricating lifecycle facts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, assert_never, cast

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import (
    AdoptRevision,
    BlockRequest,
    CancelOwnedJob,
    CancelOwnedResource,
    CancelTurn,
    CloseAttemptScope,
    CloseSession,
    CollectEvidence,
    ContractError,
    DiscardWorkspace,
    DispatchTurn,
    EnsureSession,
    EnsureWorkspace,
    ExecuteRegisteredOperation,
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
    SnapshotAndRetain,
    SnapshotAndRetainRun,
    SubmitMeasurement,
    TurnPermit,
    Until,
    VerifyAdoption,
)
from vs_core.api.recovery import OwnerEvent

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
    """What an executor is told of one attempt of a request, besides the request itself.

    The run lock makes one process the only writer, so the context carries no host authority.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, arbitrary_types_allowed=True
    )
    now_at: float = Field(ge=0, allow_inf_nan=False)
    payload_digest: str = Field(min_length=1)
    until: Until
    """When core ends the request: the run-clock time, and the watches that end it sooner.

    An executor may use it to bound its own work. It need not enforce it: the loop cancels
    a request that is still running once the time passes or a watch fires, and core
    reports the request as ``Failed``.
    """
    permit: TurnPermit | None = None
    """The logical-turn permit core issued for this request, when the request consumes one."""
    repeat: bool = False
    """Whether an earlier attempt of this request may have run.

    The runner loop records an attempt before any of its I/O, so an attempt after the first
    proves an earlier one may have started. A repeat must reach the end a first run reaches
    without doing twice what the earlier attempt may have done.
    """


def payload_digest_of(request: RequestBase) -> str:
    """The digest that names *request*'s payload in an ``ExecutionContext``."""
    return hashlib.sha256(request.model_dump_json().encode()).hexdigest()


class ExecutionResult(BaseModel):
    """The observation and owner events commit durably together; executors never mutate core.

    The shell records the owner events in the same commit as the observation,
    then applies them in order, so a crash cannot lose any of them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    observation: RequestObserved
    owner_events: tuple[OwnerEvent, ...] = ()


class ExecutorRefusal(BaseModel):
    """Named missing implementation, carrying no acceptance or release proof."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    request_id: RequestId
    role: ExecutorRole
    detail: str


type ExecutionOutcome = ExecutionResult | ExecutorRefusal


# Role request unions are explicit: a shared alias such as WorkspaceRequest also
# names requests that belong to another role, which the dispatch cast would hide.
type WorkspaceRoleRequest = (
    EnsureWorkspace
    | RestoreRevision
    | SnapshotAndRetain
    | RetainRevision
    | DiscardWorkspace
    | AdoptRevision
    | VerifyAdoption
    | SnapshotAndRetainRun
)
type SessionRoleRequest = (
    EnsureSession | DispatchTurn | InspectTurn | CancelTurn | CloseSession | ResumeSessionTurn
)
type EvaluationRoleRequest = (
    SubmitMeasurement
    | ObserveOwnedJob
    | InspectOwnedJob
    | CancelOwnedJob
    | CollectEvidence
    | CloseAttemptScope
)
type OperationRoleRequest = ExecuteRegisteredOperation | InspectRequest | CancelOwnedResource


class WorkspaceRequests(Protocol):
    """D2 translates workspace, checkpoint and adoption requests."""

    async def execute(
        self, request: WorkspaceRoleRequest, context: ExecutionContext
    ) -> ExecutionOutcome: ...


class SessionRequests(Protocol):
    """B translates canonical session requests through vs-agent interfaces."""

    async def execute(
        self, request: SessionRoleRequest, context: ExecutionContext
    ) -> ExecutionOutcome: ...


class EvaluationRequests(Protocol):
    """C translates jobs, evidence and scope closure through vs-evaluation."""

    async def execute(
        self, request: EvaluationRoleRequest, context: ExecutionContext
    ) -> ExecutionOutcome: ...


class OperationExecutor(Protocol):
    """Closed registered-operation catalog and exact target inspection/cancel."""

    async def execute(
        self, request: OperationRoleRequest, context: ExecutionContext
    ) -> ExecutionOutcome: ...


class SemanticEvents(Protocol):
    """Committed diagnostic publication, deduplicated by canonical request ID."""

    async def execute(
        self, request: BlockRequest, context: ExecutionContext
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
        BlockRequest: ExecutorRole.SEMANTIC_EVENTS,
    }
)


def counts_toward_concurrency(request: Request) -> bool:
    """Whether the request occupies one of the shell's concurrent slots.

    A cancellation never waits for a free slot: it exists to end work that holds one, so
    a full set of running turns must not keep it from starting.
    """
    return not isinstance(request, CancelTurn | CancelOwnedJob | CancelOwnedResource)


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
        return _unbound_refusal(request.request_id, self.role)


def _unbound_refusal(request_id: RequestId, role: ExecutorRole) -> ExecutorRefusal:
    return ExecutorRefusal(
        request_id=request_id,
        role=role,
        detail=f"{role.value} executor not bound; owning lane must supply implementation",
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

    def refusal(self, request: Request) -> ExecutorRefusal | None:
        """Typed refusal when the request's role has no bound translator, else None.

        The shell asks before it commits dispatch authorization, so an unbound
        role never leaves a DISPATCHED record behind.
        """
        role = self.role_of(request)
        if not isinstance(getattr(self, role.value), RefusingRequestExecution):
            return None
        if request.request_id is None:
            message = "request_id: execution requires a canonical identity"
            raise ValueError(message)
        return _unbound_refusal(request.request_id, role)

    @staticmethod
    def role_of(request: Request) -> ExecutorRole:
        """Owning role by exact closed variant; unmapped subclasses are a typed error."""
        role = REQUEST_DISPATCH.get(type(request))
        if role is None:
            raise ContractError(
                ("request", "kind"), f"no executor role for request type {type(request).__name__}"
            )
        return role

    async def dispatch(self, request: Request, context: ExecutionContext) -> ExecutionOutcome:
        """Dispatch by exact closed variant, with statically narrowed role input."""
        role = self.role_of(request)
        match role:
            case ExecutorRole.WORKSPACES:
                return await self.workspaces.execute(cast("WorkspaceRoleRequest", request), context)
            case ExecutorRole.SESSIONS:
                return await self.sessions.execute(cast("SessionRoleRequest", request), context)
            case ExecutorRole.EVALUATION:
                return await self.evaluation.execute(
                    cast("EvaluationRoleRequest", request), context
                )
            case ExecutorRole.OPERATIONS:
                return await self.operations.execute(cast("OperationRoleRequest", request), context)
            case ExecutorRole.SEMANTIC_EVENTS:
                return await self.semantic_events.execute(cast("BlockRequest", request), context)
            case _:
                assert_never(role)
