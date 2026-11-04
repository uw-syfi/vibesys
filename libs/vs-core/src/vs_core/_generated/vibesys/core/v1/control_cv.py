# Generated from vibesys/core/v1/control.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations

from enum import StrEnum
from functools import partial
from typing import Annotated, ClassVar, Literal, final

from pydantic import AfterValidator, ConfigDict, Field, field_serializer
from pydantic.dataclasses import dataclass

from vs_core._corevalue import (
    CoreValue,
    FrozenMap,
    TimestampValue,
    bounded_integer,
    cel_integer,
    omit_unset,
    resolve_value,
)
from vs_core._generated.vibesys.core.v1.common_cv import (
    ArtifactRef,
    AttemptId,
    ControlAction,
    ControlId,
    InputId,
    InputMode,
    InvocationRef,
    ItemId,
    PauseOwner,
    PoolId,
    ResumeWhen,
    RoleId,
    Scope,
    StopMode,
)
from vs_core._retry_policy import RequestRetryPolicy

MAX_REQUEST_RETRIES = 10


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class ScopeInputTarget(CoreValue):
    "Input for a scope, permitted before any invocation exists."

    model_fields: ClassVar[tuple[str, ...]] = ("kind", "scope")
    kind: Literal["scope"] = "scope"
    scope: Scope

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class ItemInputTarget(CoreValue):
    "Input for an item, permitted before any invocation exists."

    model_fields: ClassVar[tuple[str, ...]] = ("kind", "item_id")
    kind: Literal["item"] = "item"
    item_id: ItemId

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class InvocationInputTarget(CoreValue):
    "Input for one exact invocation, never silently retargeted."

    model_fields: ClassVar[tuple[str, ...]] = ("kind", "invocation")
    kind: Literal["invocation"] = "invocation"
    invocation: InvocationRef

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class RoleInputTarget(CoreValue):
    "Input for the next session turn of one role the search declares.\n\nThe role is the session's ``role_id``; core rejects a role its search does not declare."

    model_fields: ClassVar[tuple[str, ...]] = ("kind", "role_id")
    kind: Literal["role"] = "role"
    role_id: RoleId

    __hash__ = CoreValue.cached_hash


type InputTarget = Annotated[
    ScopeInputTarget | ItemInputTarget | InvocationInputTarget | RoleInputTarget,
    Field(discriminator="kind"),
]


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class PoolCapacity(CoreValue):
    "Scheduling-owned bound for one named capacity pool.\n\nUnconfigured pools retain exclusive capacity one. Global max_parallel is\nalways an additional bound; a pool capacity never grants unbounded slots."

    model_fields: ClassVar[tuple[str, ...]] = ("pool_id", "capacity")
    pool_id: PoolId
    capacity: Annotated[int, Field(ge=1)]

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class RequestRetries(RequestRetryPolicy, CoreValue):
    "How core re-sends a request its executor said it did not run. Declared by the backend.\n\n``limit`` re-sends follow the first attempt, the n-th after ``backoff * 2**(n-1)`` seconds\nof core time, never more than ``backoff_cap``. Core holds no default for any field and\nrefuses a ``limit`` above ``MAX_REQUEST_RETRIES``."

    model_fields: ClassVar[tuple[str, ...]] = ("limit", "backoff", "backoff_cap")
    limit: Annotated[
        int,
        Field(ge=0),
        AfterValidator(
            partial(bounded_integer, minimum=-9223372036854775808, maximum=9223372036854775807)
        ),
    ]
    backoff: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    backoff_cap: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]

    def __post_init__(self) -> None:
        """Enforce the proto message rules at construction as well as decode."""
        if not (self.limit <= 10):
            raise ValueError("limit above the hard cap 10")
        if not (self.backoff_cap >= self.backoff):
            raise ValueError("backoff_cap below backoff")

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class Limits(CoreValue):
    "Run bounds; every field is explicit, one field per meaning, no default.\n\nBudget (the run's own): ``max_attempts``, ``max_turns``, ``max_parallel``,\n``pool_capacities``, ``max_refunds`` and ``max_adoption_rounds``. How often a reply is corrected or a lost turn\nasked again is the role's (`Role.corrections`, `Role.lost_turns`); core keeps only\n`MAX_REASK_CHAIN`.\nDeclared by the backend and applied by core: ``request_retries``. Core's own bounds on its\nwaits and cleanup: the remaining ``*_bound`` and ``observe_*`` fields."

    model_fields: ClassVar[tuple[str, ...]] = (
        "max_attempts",
        "max_turns",
        "max_parallel",
        "pool_capacities",
        "max_refunds",
        "max_adoption_rounds",
        "request_retries",
        "cancellation_bound",
        "cleanup_bound",
        "reconciliation_bound",
        "observe_interval",
        "observe_backoff_cap",
        "queue_wait_budget",
        "queue_notice_interval",
    )
    max_attempts: Annotated[int, Field(ge=0)]
    max_turns: Annotated[int, Field(ge=0)]
    max_parallel: Annotated[int, Field(ge=1)]
    pool_capacities: tuple[PoolCapacity, ...]
    max_refunds: Annotated[int, Field(ge=0)]
    max_adoption_rounds: Annotated[int, Field(ge=1)]
    request_retries: RequestRetries
    cancellation_bound: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    cleanup_bound: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    reconciliation_bound: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    observe_interval: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    observe_backoff_cap: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    queue_wait_budget: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    queue_notice_interval: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]

    def __post_init__(self) -> None:
        """Enforce the proto message rules at construction as well as decode."""
        if not (self.observe_backoff_cap >= self.observe_interval):
            raise ValueError("observe_backoff_cap below observe_interval")
        if not (
            len(set(p.pool_id.root for p in self.pool_capacities)) == len(self.pool_capacities)
        ):
            raise ValueError("pool_capacities duplicate pool_id")

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class RunPause(CoreValue):
    "Why a paused run is paused, and whether the operator asked it to resume itself."

    model_fields: ClassVar[tuple[str, ...]] = ("owner", "capacity_wait")
    owner: PauseOwner
    capacity_wait: bool = False

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class ControlInput(CoreValue):
    "Control input lifecycle contract."

    model_fields: ClassVar[tuple[str, ...]] = (
        "control_id",
        "action",
        "mode",
        "artifact",
        "target",
        "input_mode",
        "withdraws",
        "owner",
        "when",
        "fallback",
        "capacity_wait_stands",
        "attempt",
        "item",
        "max_parallel",
    )
    control_id: ControlId
    action: ControlAction
    mode: StopMode | None = None
    artifact: ArtifactRef | None = None
    target: InputTarget | None = None
    input_mode: InputMode | None = None
    withdraws: InputId | None = None
    owner: PauseOwner | None = None
    when: ResumeWhen | None = None
    fallback: bool = False
    capacity_wait_stands: bool = False
    attempt: AttemptId | None = None
    item: ItemId | None = None
    max_parallel: Annotated[int | None, Field(ge=1)] = None

    def __post_init__(self) -> None:
        """Enforce the proto message rules at construction as well as decode."""
        if not (
            (
                not (self.artifact is not None)
                or {
                    "pause": 1,
                    "resume": 2,
                    "stop": 3,
                    "steer": 4,
                    "withdraw": 5,
                    "cancel_attempt": 6,
                    "cancel_item": 7,
                    "set_max_parallel": 8,
                    "park": 9,
                    "pause_now": 10,
                }.get(self.action, 0)
                in [4, 6, 7, 8]
            )
            and (
                not (
                    {
                        "pause": 1,
                        "resume": 2,
                        "stop": 3,
                        "steer": 4,
                        "withdraw": 5,
                        "cancel_attempt": 6,
                        "cancel_item": 7,
                        "set_max_parallel": 8,
                        "park": 9,
                        "pause_now": 10,
                    }.get(self.action, 0)
                    in [6, 7, 8]
                )
                or (self.artifact is not None)
            )
        ):
            raise ValueError(
                "artifact only for a steer or operator action, and required for operator actions"
            )
        if not (
            (
                {
                    "pause": 1,
                    "resume": 2,
                    "stop": 3,
                    "steer": 4,
                    "withdraw": 5,
                    "cancel_attempt": 6,
                    "cancel_item": 7,
                    "set_max_parallel": 8,
                    "park": 9,
                    "pause_now": 10,
                }.get(self.action, 0)
                == 3
            )
            == ({"drain": 1, "cancel": 2}.get(self.mode, 0) != 0)
        ):
            raise ValueError("mode required for its action and only for it")
        if not (
            (
                {
                    "pause": 1,
                    "resume": 2,
                    "stop": 3,
                    "steer": 4,
                    "withdraw": 5,
                    "cancel_attempt": 6,
                    "cancel_item": 7,
                    "set_max_parallel": 8,
                    "park": 9,
                    "pause_now": 10,
                }.get(self.action, 0)
                == 5
            )
            == (self.withdraws is not None)
        ):
            raise ValueError("withdraws required for its action and only for it")
        if not (
            (
                {
                    "pause": 1,
                    "resume": 2,
                    "stop": 3,
                    "steer": 4,
                    "withdraw": 5,
                    "cancel_attempt": 6,
                    "cancel_item": 7,
                    "set_max_parallel": 8,
                    "park": 9,
                    "pause_now": 10,
                }.get(self.action, 0)
                == 1
            )
            == ({"operator": 1, "quota": 2}.get(self.owner, 0) != 0)
        ):
            raise ValueError("owner required for its action and only for it")
        if not (
            (
                {
                    "pause": 1,
                    "resume": 2,
                    "stop": 3,
                    "steer": 4,
                    "withdraw": 5,
                    "cancel_attempt": 6,
                    "cancel_item": 7,
                    "set_max_parallel": 8,
                    "park": 9,
                    "pause_now": 10,
                }.get(self.action, 0)
                == 2
            )
            == ({"now": 1, "capacity_returns": 2}.get(self.when, 0) != 0)
        ):
            raise ValueError("when required for its action and only for it")
        if not (
            (
                {
                    "pause": 1,
                    "resume": 2,
                    "stop": 3,
                    "steer": 4,
                    "withdraw": 5,
                    "cancel_attempt": 6,
                    "cancel_item": 7,
                    "set_max_parallel": 8,
                    "park": 9,
                    "pause_now": 10,
                }.get(self.action, 0)
                == 6
            )
            == (self.attempt is not None)
        ):
            raise ValueError("attempt required for its action and only for it")
        if not (
            (
                {
                    "pause": 1,
                    "resume": 2,
                    "stop": 3,
                    "steer": 4,
                    "withdraw": 5,
                    "cancel_attempt": 6,
                    "cancel_item": 7,
                    "set_max_parallel": 8,
                    "park": 9,
                    "pause_now": 10,
                }.get(self.action, 0)
                == 7
            )
            == (self.item is not None)
        ):
            raise ValueError("item required for its action and only for it")
        if not (
            (
                {
                    "pause": 1,
                    "resume": 2,
                    "stop": 3,
                    "steer": 4,
                    "withdraw": 5,
                    "cancel_attempt": 6,
                    "cancel_item": 7,
                    "set_max_parallel": 8,
                    "park": 9,
                    "pause_now": 10,
                }.get(self.action, 0)
                == 8
            )
            == (self.max_parallel is not None)
        ):
            raise ValueError("max_parallel required for its action and only for it")
        if not (
            not (self.target is not None)
            or {
                "pause": 1,
                "resume": 2,
                "stop": 3,
                "steer": 4,
                "withdraw": 5,
                "cancel_attempt": 6,
                "cancel_item": 7,
                "set_max_parallel": 8,
                "park": 9,
                "pause_now": 10,
            }.get(self.action, 0)
            == 4
        ):
            raise ValueError("target: only a steer has a target")
        if not (
            {"next-turn": 1, "interrupt": 2, "standing": 3}.get(self.input_mode, 0) == 0
            or {
                "pause": 1,
                "resume": 2,
                "stop": 3,
                "steer": 4,
                "withdraw": 5,
                "cancel_attempt": 6,
                "cancel_item": 7,
                "set_max_parallel": 8,
                "park": 9,
                "pause_now": 10,
            }.get(self.action, 0)
            == 4
        ):
            raise ValueError("input_mode only for steer")
        if not (
            not ({"next-turn": 1, "interrupt": 2, "standing": 3}.get(self.input_mode, 0) in [2, 3])
            or not (self.target is not None)
            or not isinstance(self.target, InvocationInputTarget)
        ):
            raise ValueError("input_mode interrupt or standing cannot target an invocation")
        if not (
            not (self.fallback or self.capacity_wait_stands)
            or {"now": 1, "capacity_returns": 2}.get(self.when, 0) == 1
        ):
            raise ValueError("when only immediate resume switches or keeps")
        if not (not (self.fallback and self.capacity_wait_stands)):
            raise ValueError("capacity_wait_stands fallback ends wait")

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class RunPark(CoreValue):
    "Durable drain authority. A parked run can resume only in a later host epoch."

    model_fields: ClassVar[tuple[str, ...]] = (
        "admitted_turns",
        "immediate",
        "deadline_at",
        "control_id",
        "epoch",
    )
    admitted_turns: tuple[InvocationRef, ...] = ()
    immediate: bool = False
    deadline_at: Annotated[float | None, Field(allow_inf_nan=False)] = None
    control_id: ControlId
    epoch: Annotated[int, Field(ge=0)]

    __hash__ = CoreValue.cached_hash


resolve_value(ScopeInputTarget)
resolve_value(ItemInputTarget)
resolve_value(InvocationInputTarget)
resolve_value(RoleInputTarget)
resolve_value(PoolCapacity)
resolve_value(RequestRetries)
resolve_value(Limits)
resolve_value(RunPause)
resolve_value(ControlInput)
resolve_value(RunPark)
