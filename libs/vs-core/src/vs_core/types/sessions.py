"""Session lifetime, invocation acceptance and continuation fencing."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from .common import (
    ArtifactRef,
    ContinuationId,
    Count,
    ExecuteRegisteredOperation,
    Generation,
    InvocationId,
    InvocationRef,
    Observation,
    OperationId,
    RequestBase,
    RequestId,
    RoleId,
    SchemaRef,
    Scope,
    Seconds,
    SessionId,
    Value,
    WorkspaceRef,
)


class Access(StrEnum):
    """Access lifecycle contract."""

    READ_ONLY = "read-only"
    WRITE_ARTIFACTS = "write-artifacts"
    WRITE_CANDIDATE = "write-candidate"


class SessionSpec(Value):
    """Session spec lifecycle contract."""

    session_id: SessionId
    role_id: RoleId
    policy: Literal["fresh", "reuse"]
    lifetime: Literal["ephemeral", "owner"]
    access: Access


class TurnSpec(Value):
    """Turn spec lifecycle contract."""

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
    """Session view lifecycle contract."""

    spec: SessionSpec
    scope: Scope
    generation: Generation
    phase: SessionPhase
    invocation: InvocationId | None = None
    accepted: bool = False
    reserved_inputs: tuple[ArtifactRef, ...] = ()
    pending_intents: tuple[RequestId, ...] = ()
    acceptance_sequence: int | None = Field(default=None, ge=0)
    continuation_id: ContinuationId | None = None


class Invocation(Value):
    """Durable invocation history required for assessment and callback finality."""

    invocation: InvocationRef
    scope: Scope
    turn: TurnSpec
    registered_operation: OperationId | None = None
    phase: SessionPhase
    observation: Observation | None = None
    output_schema: SchemaRef | None = None
    output_json: str | None = None
    reserved_inputs: tuple[ArtifactRef, ...] = ()


class SessionsState(Value):
    """Sessions state lifecycle contract."""

    sessions: tuple[SessionView, ...] = ()
    invocations: tuple[Invocation, ...] = ()


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
    inputs: tuple[ArtifactRef, ...]


class InterruptRequested(Value):
    """Interrupt requested lifecycle contract."""

    kind: Literal["interrupt_requested"] = "interrupt_requested"
    invocation: InvocationRef
    refund: Count = 0


class TurnResult(Value):
    """Turn result lifecycle contract."""

    kind: Literal["turn_result"] = "turn_result"
    invocation: InvocationRef
    observation: Observation
    output_schema: SchemaRef | None = None
    output_json: str | None = None


class EnsureSession(RequestBase):
    """Ensure session lifecycle contract."""

    kind: Literal["ensure_session"] = "ensure_session"
    spec: SessionSpec


class DispatchTurn(RequestBase):
    """Dispatch turn lifecycle contract."""

    kind: Literal["dispatch_turn"] = "dispatch_turn"
    turn: TurnSpec


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
    continuation_id: ContinuationId


type SessionsEvent = Annotated[
    RegisteredTurnRequested
    | TurnRequested
    | TurnObserved
    | SessionObserved
    | SteerReceived
    | InterruptRequested,
    Field(discriminator="kind"),
]
type SessionRequest = Annotated[
    EnsureSession | DispatchTurn | InspectTurn | CancelTurn | CloseSession | ResumeSessionTurn,
    Field(discriminator="kind"),
]
