"""Closed decisions, library-owned operations and pure strategy contract."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    SerializeAsAny,
    ValidationInfo,
    field_serializer,
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
    RevisionRef,
    RoleId,
    SchemaRef,
    Scope,
    Seconds,
    StrategyId,
    Value,
    unset,
)
from .evaluation import MeasurementIdentity, MeasurementPlan
from .sessions import SessionSpec, TurnSpec
from .settlement import AssessmentProposal, RunResultProposal, Selection
from .until import Within


class DecisionBase(Value):
    """Decision base lifecycle contract."""

    decision_id: DecisionId
    scope: Scope
    """Who owns the work. ``THIS_RUN`` means the run itself; core fills in its own scope."""


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


class Cancel(Value):
    """Cancel lifecycle contract."""

    kind: Literal["cancel"] = "cancel"


class Interrupt(Value):
    """Interrupt lifecycle contract."""

    kind: Literal["interrupt"] = "interrupt"
    refund: Count = 0


class Settle(Value):
    """Strategy assessment and retention proposal with an explicit candidate.

    The kernel forwards candidate unchanged, including None. It cannot choose a
    latest checkpoint on the strategy's behalf. Eligibility requires declared
    evidence and assessment authority and completed resource cleanup; WIP alone
    never establishes candidate eligibility.
    """

    kind: Literal["settle"] = "settle"
    assessments: tuple[AssessmentProposal, ...]
    eligible: bool
    retention: Literal["discard", "wip", "candidate"]
    outcome: Literal["succeeded", "failed", "cancelled", "blocked"]
    candidate: RevisionRef | None = None


type Disposition = Annotated[Cancel | Settle | Interrupt, Field(discriminator="kind")]


class Withdraw(DecisionBase):
    """Withdraw lifecycle contract."""

    kind: Literal["withdraw"] = "withdraw"
    target: AttemptRef | InvocationRef | OperationRef
    disposition: Disposition


class Measure(DecisionBase):
    """Measure lifecycle contract."""

    kind: Literal["measure"] = "measure"
    plan: MeasurementPlan
    compared: bool = Field(default=False, exclude_if=unset)
    """The strategy will read the candidate's comparison with its base.

    Core then compares the candidate with the attempt's own base, taking the base's run beside
    the candidate's when no fresh sample of it is held. A measurement nobody compares costs no
    base run.
    """


class ProposeWinner(DecisionBase):
    """Propose winner lifecycle contract."""

    kind: Literal["propose_winner"] = "propose_winner"
    selection: Selection


class Finish(DecisionBase):
    """End the run with a result, draining or cancelling what is still in flight."""

    kind: Literal["finish"] = "finish"
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
    normalized_turn: TurnSpec | None = None
    normalized_measurement: MeasurementIdentity | None = None

    _registered_wire: OperationWire | None = PrivateAttr(default=None)
    _registered_turn: TurnSpec | None = PrivateAttr(default=None)
    _registered_measurement: MeasurementIdentity | None = PrivateAttr(default=None)

    @property
    def registered_wire(self) -> OperationWire | None:
        """Value-only codec proof established at the registered ingress boundary."""
        return self._registered_wire

    @property
    def registered_turn(self) -> TurnSpec | None:
        """Normalized turn proof bound to the registered input."""
        return self._registered_turn

    @property
    def registered_measurement(self) -> MeasurementIdentity | None:
        """Expected measurement proof bound to the exact registered input."""
        return self._registered_measurement

    @model_validator(mode="after")
    def validate_registered_model(self, info: ValidationInfo) -> Operation:
        """Normalize proposals; validate durable facts without rewriting identity."""
        if info.context and "operation_registry" in info.context:
            wire = info.context["operation_registry"].encode(self.request)
            turn = info.context["operation_registry"].normalize_turn(self.request)
            measurement = info.context["operation_registry"].normalize_measurement(self.request)
            normalizations = {
                "normalized_turn": turn,
                "normalized_measurement": measurement,
            }
            persisted = info.context.get("persisted_operation")
            if persisted:
                for field, normalized in normalizations.items():
                    if getattr(self, field) != normalized:
                        raise OperationCodecError(
                            ("operation", field),
                            "durable normalization differs; explicit migration required",
                        )
            validated = self.model_copy(update={} if persisted else normalizations)
            object.__setattr__(
                validated,
                "__pydantic_private__",
                {
                    **(self.__pydantic_private__ or {}),
                    "_registered_wire": wire,
                    "_registered_turn": turn,
                    "_registered_measurement": measurement,
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


type Work = Annotated[
    RequestTurn | StartAttempt | Withdraw | Measure | ProposeWinner | Operation,
    Field(discriminator="kind"),
]
"""What a search can ask for. It only starts inside a ``Wait``."""


class Wait(Value):
    """Start ``request`` and wait for its result, at most ``until``.

    This is the only way a search starts work. A bare request is not a ``Decision``, and
    ``until`` is required: every wait has a limit.
    """

    kind: Literal["wait"] = "wait"
    request: Work
    until: Within


class WakeAt(Value):
    """Ask to be called again at a run-clock time even if nothing else happens."""

    kind: Literal["wake_at"] = "wake_at"
    time: Seconds


type Decision = Annotated[Wait | WakeAt | Finish, Field(discriminator="kind")]
"""What ``Search.init`` and ``Search.update`` return."""

type Submission = Annotated[Wait | Finish, Field(discriminator="kind")]
"""What the kernel accepts from a search: a ``Wait`` (with its ``until``) or a ``Finish``."""

type Command = Annotated[
    RequestTurn | StartAttempt | Withdraw | Measure | ProposeWinner | Operation | Finish,
    Field(discriminator="kind"),
]
"""What the kernel acts on: a ``Work`` or the ``Finish``. ``Wait`` supplies the ``until``."""


class StrategyState(Value):
    """Strategy state lifecycle contract."""

    schema_version: int = Field(ge=1)


class SearchDeclaration(Value):
    """What core derives about a search: its identity and what it needs from the host.

    A search author declares none of this. ``@search`` names the search, and the rest
    (the operations its namespace owns, the lifecycle capabilities it requires) comes from the
    registration; the event kinds it hears and the work it starts are whatever its flows do.
    """

    strategy_id: StrategyId
    required_operations: tuple[OperationSchemaRef, ...] = ()
    optional_operations: tuple[OperationSchemaRef, ...] = ()
    required: frozenset[LifecycleCapability] = frozenset()
    optional: frozenset[LifecycleCapability] = frozenset()
    roles: tuple[RoleId, ...] = ()
    """The role ids of the sessions the search's turns run in, sorted; an input may name one.

    Recorded when the run starts and kept for its life: a run resumed under code that adds,
    renames or removes a role still accepts inputs for the roles it started with.
    """

    def resumable_as(self, offered: SearchDeclaration) -> bool:
        """Whether a run that recorded this declaration may resume under ``offered``.

        Every field must match except ``roles``, which follow the code's agents and may change
        with it; the run keeps the roles it recorded.
        """
        return self.model_copy(update={"roles": ()}) == offered.model_copy(update={"roles": ()})

    @property
    def state_schema(self) -> SchemaRef:
        """The schema of the persisted flow state, named by the search."""
        return SchemaRef(name=f"{self.strategy_id.root}.flow-state", version=1)

    @field_serializer("required", "optional", when_used="json")
    def _sorted_sets(self, value: frozenset[str]) -> list[str]:
        """Write the sets in sorted order, as `Capabilities.lifecycle` does."""
        return sorted(value)


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
