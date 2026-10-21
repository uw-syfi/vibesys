# Generated from vibesys/core/v1/common.proto by protoc-gen-corevalue. DO NOT EDIT.
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

IDENTITY_PATTERN = "^\\S+$"


class RunStatus(StrEnum):
    "Run status lifecycle contract."

    RUNNING = "running"
    PAUSED = "paused"
    CLOSING = "closing"
    TERMINAL = "terminal"
    BLOCKED = "blocked"
    DRAINING = "draining"
    PARKED = "parked"


class PauseOwner(StrEnum):
    "Who paused the run, which decides what may end the pause."

    OPERATOR = "operator"
    QUOTA = "quota"


class StopMode(StrEnum):
    "How a stop treats work in flight: let it finish, or cancel it."

    DRAIN = "drain"
    CANCEL = "cancel"


ResumeWhen = Literal["now", "capacity_returns"]


class InputMode(StrEnum):
    "How an input reaches the turns of its target; interrupt metadata grants no refund authority.\n\n``NEXT_TURN`` is reserved by the first matching turn and delivered to it once.\n``INTERRUPT`` is first offered to a matching session turn that is running, and joins it\nwhen the provider takes it; otherwise, or once the provider takes it back, the next\nmatching turn reserves it as a next-turn input. Delivery is at least once: an offer a\ncrash leaves unresolved is offered again, and may reach the turn twice.\n``STANDING`` is never reserved: every matching session turn dispatched while it is in force\nreceives it, until it is withdrawn or its target ends."

    NEXT_TURN = "next-turn"
    INTERRUPT = "interrupt"
    STANDING = "standing"


type ControlAction = Literal[
    "pause",
    "resume",
    "stop",
    "steer",
    "withdraw",
    "cancel_attempt",
    "cancel_item",
    "set_max_parallel",
    "park",
]


@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class Identity(CoreValue):
    "Canonical tagged identity; domains survive JSON union round trips."

    model_fields: ClassVar[tuple[str, ...]] = ("root",)
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class RunId(Identity):
    "Distinct run identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["run"] = "run"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class AttemptId(Identity):
    "Distinct attempt identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["attempt"] = "attempt"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class ItemId(Identity):
    "Distinct item identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["item"] = "item"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class SessionId(Identity):
    "Distinct session identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["session"] = "session"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class InvocationId(Identity):
    "Distinct invocation identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["invocation"] = "invocation"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class ContinuationId(Identity):
    "Distinct continuation identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["continuation"] = "continuation"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class InputId(Identity):
    "Identity of one input occurrence, independent of content digest."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["input"] = "input"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class EvidenceId(Identity):
    "Distinct evidence identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["evidence"] = "evidence"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class DecisionId(Identity):
    "Distinct decision identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["decision"] = "decision"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class RequestId(Identity):
    "Distinct request identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["request"] = "request"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class EventId(Identity):
    "Distinct event identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["event"] = "event"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class SettlementId(Identity):
    "Distinct settlement identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["settlement"] = "settlement"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class StrategyId(Identity):
    "Distinct strategy identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["strategy"] = "strategy"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class OperationId(Identity):
    "Distinct operation identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["operation"] = "operation"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class ArtifactId(Identity):
    "Distinct artifact identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["artifact"] = "artifact"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class ResourceId(Identity):
    "Distinct lease episode, never a reusable raw physical backend identity.\n\nThe owning library's normalization episode-qualifies reused physical IDs;\nconflicting scope or admission claims cannot reuse one core ResourceId."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["resource"] = "resource"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class RevisionId(Identity):
    "Distinct revision identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["revision"] = "revision"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class RoleId(Identity):
    "Distinct role identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["role"] = "role"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class PoolId(Identity):
    "Distinct pool identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["pool"] = "pool"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class ChargeId(Identity):
    "Distinct charge identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["charge"] = "charge"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class ControlId(Identity):
    "Distinct control identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["control"] = "control"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class HostId(Identity):
    "Distinct host identity."

    model_fields: ClassVar[tuple[str, ...]] = ("root", "kind")
    root: Annotated[str, Field(min_length=1, pattern="^\\S+$")]
    kind: Literal["host"] = "host"

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class SchemaRef(CoreValue):
    "Schema ref lifecycle contract."

    model_fields: ClassVar[tuple[str, ...]] = ("name", "version")
    name: Annotated[str, Field(min_length=1)]
    version: Annotated[int, Field(ge=1)]

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class ArtifactRef(CoreValue):
    "Artifact ref lifecycle contract."

    model_fields: ClassVar[tuple[str, ...]] = ("artifact_id", "digest", "schema_ref")
    artifact_id: ArtifactId
    digest: Annotated[str, Field(min_length=1)]
    schema_ref: SchemaRef | None = None

    __hash__ = CoreValue.cached_hash


type ScopeOwner = Annotated[RunId | AttemptId, Field(discriminator="kind")]


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class Scope(CoreValue):
    "Scope lifecycle contract."

    model_fields: ClassVar[tuple[str, ...]] = ("owner", "generation")
    owner: ScopeOwner
    generation: Annotated[int, Field(ge=0)]

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class InvocationRef(CoreValue):
    "Invocation ref lifecycle contract."

    model_fields: ClassVar[tuple[str, ...]] = ("session_id", "invocation_id", "generation")
    session_id: SessionId
    invocation_id: InvocationId
    generation: Annotated[int, Field(ge=0)]

    __hash__ = CoreValue.cached_hash


resolve_value(Identity)
resolve_value(RunId)
resolve_value(AttemptId)
resolve_value(ItemId)
resolve_value(SessionId)
resolve_value(InvocationId)
resolve_value(ContinuationId)
resolve_value(InputId)
resolve_value(EvidenceId)
resolve_value(DecisionId)
resolve_value(RequestId)
resolve_value(EventId)
resolve_value(SettlementId)
resolve_value(StrategyId)
resolve_value(OperationId)
resolve_value(ArtifactId)
resolve_value(ResourceId)
resolve_value(RevisionId)
resolve_value(RoleId)
resolve_value(PoolId)
resolve_value(ChargeId)
resolve_value(ControlId)
resolve_value(HostId)
resolve_value(SchemaRef)
resolve_value(ArtifactRef)
resolve_value(Scope)
resolve_value(InvocationRef)
