# Generated from vibesys/core/v1/contracts.proto by protoc-gen-corevalue. DO NOT EDIT.
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
from vs_core._generated.vibesys.core.v1.common_cv import SchemaRef
from vs_core._generated.vibesys.core.v1.vocabulary_cv import LifecycleClass

type NullValue = None

type PureOptionValue = str | int | Annotated[float, Field(allow_inf_nan=False)] | bool | NullValue


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class PureOption(CoreValue):
    "Pure option lifecycle contract."

    model_fields: ClassVar[tuple[str, ...]] = ("key", "value")
    key: Annotated[str, Field(min_length=1)]
    value: PureOptionValue

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(
        extra="forbid",
        strict=True,
        validate_default=True,
        revalidate_instances="subclass-instances",
    ),
)
class OperationSchemaRef(CoreValue):
    "Operation schema ref lifecycle contract."

    model_fields: ClassVar[tuple[str, ...]] = (
        "kind",
        "request_schema",
        "outcome_schema",
        "lifecycle",
    )
    kind: Annotated[str, Field(min_length=1)]
    request_schema: SchemaRef
    outcome_schema: SchemaRef
    lifecycle: LifecycleClass

    __hash__ = CoreValue.cached_hash


resolve_value(PureOption)
resolve_value(OperationSchemaRef)
