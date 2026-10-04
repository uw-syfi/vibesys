"""Closed decisions, library-owned operations and pure strategy contract."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    SerializeAsAny,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .attempts import AttemptBudget, WorkspacePlan
from .common import (
    AttemptId,
    AttemptRef,
    Count,
    DecisionId,
    DependencyRef,
    InvocationRef,
    ItemId,
    LifecycleCapability,
    LifecycleClass,
    OperationRef,
    OperationSchemaRef,
    OperationWire,
    RejectionCode,
    RequestId,
    SchemaRef,
    Scope,
    Seconds,
    StrategyId,
    Value,
)
from .evaluation import MeasurementPlan
from .sessions import SessionSpec, TurnSpec
from .settlement import AssessmentProposal, RunResultProposal, Selection


class DecisionBase(Value):
    """Decision base lifecycle contract."""

    decision_id: DecisionId
    scope: Scope
    depends_on: tuple[DecisionId, ...] = ()


class RequestTurn(DecisionBase):
    """Request turn lifecycle contract."""

    kind: Literal["request_turn"] = "request_turn"
    turn: TurnSpec


class StartAttempt(DecisionBase):
    """Start attempt lifecycle contract."""

    kind: Literal["start_attempt"] = "start_attempt"
    attempt_id: AttemptId
    item_id: ItemId
    workspace: WorkspacePlan
    budget: AttemptBudget
    initial_sessions: tuple[SessionSpec, ...] = ()


class Park(Value):
    """Park lifecycle contract."""

    kind: Literal["park"] = "park"


class Cancel(Value):
    """Cancel lifecycle contract."""

    kind: Literal["cancel"] = "cancel"


class Interrupt(Value):
    """Interrupt lifecycle contract."""

    kind: Literal["interrupt"] = "interrupt"
    refund: Count = 0


class Settle(Value):
    """Settle lifecycle contract."""

    kind: Literal["settle"] = "settle"
    assessments: tuple[AssessmentProposal, ...]
    eligible: bool
    retention: Literal["discard", "wip", "candidate"]
    outcome: Literal["succeeded", "failed", "cancelled", "blocked"]


type Disposition = Annotated[Park | Cancel | Settle | Interrupt, Field(discriminator="kind")]


class Withdraw(DecisionBase):
    """Withdraw lifecycle contract."""

    kind: Literal["withdraw"] = "withdraw"
    target: AttemptRef | InvocationRef | OperationRef
    disposition: Disposition


class Measure(DecisionBase):
    """Measure lifecycle contract."""

    kind: Literal["measure"] = "measure"
    plan: MeasurementPlan


class ProposeWinner(DecisionBase):
    """Propose winner lifecycle contract."""

    kind: Literal["propose_winner"] = "propose_winner"
    selection: Selection


class Stop(DecisionBase):
    """Stop lifecycle contract."""

    kind: Literal["stop"] = "stop"
    mode: Literal["drain", "cancel"]
    result: RunResultProposal


class OperationRequest(Value):
    """Owning library subclasses declare Literal fields and an outcome model."""

    kind: str = Field(min_length=1)
    lifecycle: LifecycleClass
    outcome_model: ClassVar[type[BaseModel]]


class Operation(DecisionBase):
    """Operation lifecycle contract."""

    kind: Literal["operation"] = "operation"
    request: SerializeAsAny[OperationRequest]
    deadline_at: Seconds
    normalized_turn: TurnSpec | None = None

    _registered_wire: OperationWire | None = PrivateAttr(default=None)
    _registered_turn: TurnSpec | None = PrivateAttr(default=None)

    @property
    def registered_wire(self) -> OperationWire | None:
        """Value-only codec proof established at the registered ingress boundary."""
        return self._registered_wire

    @property
    def registered_turn(self) -> TurnSpec | None:
        """Normalized turn proof bound to the registered input."""
        return self._registered_turn

    @model_validator(mode="after")
    def validate_registered_model(self, info: ValidationInfo) -> Operation:
        """Bind schema validation to this constructed value, never mutate inputs."""
        if info.context and "operation_registry" in info.context:
            wire = info.context["operation_registry"].encode(self.request)
            turn = info.context["operation_registry"].normalize_turn(self.request)
            validated = self.model_copy(update={"normalized_turn": turn})
            object.__setattr__(
                validated,
                "__pydantic_private__",
                {
                    **(self.__pydantic_private__ or {}),
                    "_registered_wire": wire,
                    "_registered_turn": turn,
                },
            )
            return validated
        return self

    @field_validator("request", mode="before")
    @classmethod
    def registered_wire_request(cls, value: object, info: ValidationInfo) -> object:
        """JSON requests require the owning-library registry, never narrowing."""
        if isinstance(value, dict):
            if not info.context or "operation_registry" not in info.context:
                raise OperationCodecError(
                    ("operation",), "operation request requires registered codec context"
                )
            return info.context["operation_registry"].decode_payload(value)
        if type(value) is OperationRequest:
            raise OperationCodecError(
                ("operation",), "abstract operation request is not registered"
            )
        return value


type Decision = Annotated[
    RequestTurn | StartAttempt | Withdraw | Measure | ProposeWinner | Stop | Operation,
    Field(discriminator="kind"),
]


class StrategyState(Value):
    """Strategy state lifecycle contract."""

    schema_version: int = Field(ge=1)


class Proposal[S: StrategyState](Value):
    """Proposal lifecycle contract."""

    state: S
    decisions: tuple[Decision, ...]


class StrategyDeclaration(Value):
    """Strategy declaration lifecycle contract."""

    strategy_id: StrategyId
    state_schema: SchemaRef
    required_operations: tuple[OperationSchemaRef, ...] = ()
    optional_operations: tuple[OperationSchemaRef, ...] = ()
    required: frozenset[LifecycleCapability] = frozenset()
    optional: frozenset[LifecycleCapability] = frozenset()


class Accepted(Value):
    """Accepted lifecycle contract."""

    kind: Literal["accepted"] = "accepted"
    decision_id: DecisionId
    allocated_ids: tuple[str, ...] = ()
    request_ids: tuple[RequestId, ...] = ()
    queue_position: Count | None = None
    dependencies: tuple[DependencyRef, ...] = ()


class Rejected(Value):
    """Rejected lifecycle contract."""

    kind: Literal["rejected"] = "rejected"
    decision_id: DecisionId
    code: RejectionCode
    path: tuple[str | int, ...]
    detail: str
    retry_after: DependencyRef | None = None


type DecisionFeedback = Annotated[Accepted | Rejected, Field(discriminator="kind")]


class OperationCodecError(ValueError):
    """An operation cannot be decoded without its owning schema registry."""

    def __init__(self, path: tuple[str, ...], detail: str) -> None:
        """Report the strict wire-contract violation."""
        super().__init__(f"{path}: {detail}")
