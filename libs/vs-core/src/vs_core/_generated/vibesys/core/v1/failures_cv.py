# Generated from vibesys/core/v1/failures.proto by protoc-gen-corevalue. DO NOT EDIT.
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
from vs_core._generated.vibesys.core.v1.vocabulary_cv import ReplyErrorCode

REPLY_ERROR_MESSAGE_LIMIT = 500

type ReplyPathPart = str | int


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class ReplyError(CoreValue):
    "One typed problem in a reply: where it is and what is wrong.\n\n``message`` is the validator's own wording when it reported one (a provider's schema\ncheck does); the reply model's check leaves it empty and ``code`` says what is wrong."

    model_fields: ClassVar[tuple[str, ...]] = ("path", "code", "message")
    path: tuple[ReplyPathPart, ...]
    code: ReplyErrorCode
    message: Annotated[str, Field(max_length=500)] = ""

    __hash__ = CoreValue.cached_hash


resolve_value(ReplyError)
