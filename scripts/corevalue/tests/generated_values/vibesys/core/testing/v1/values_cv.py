from __future__ import annotations


# Generated from vibesys/core/testing/v1/values.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations

from typing import Annotated, ClassVar, Literal, final
from enum import StrEnum
from pydantic import AfterValidator, ConfigDict, Field
from functools import partial
from pydantic.dataclasses import dataclass
from vs_core._corevalue import CoreValue, FrozenMap, TimestampValue, bounded_integer


@final
@dataclass(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra='forbid', strict=True, validate_default=True))
class MapValue(CoreValue):
    'Exercised by the real plugin and protobuf-py, without a subprocess in the test.'
    model_fields: ClassVar[tuple[str, ...]] = ('counts', 'labels', 'at', 'history', 'marked')
    counts: FrozenMap[str, Annotated[int, AfterValidator(partial(bounded_integer, minimum=-9223372036854775808, maximum=9223372036854775807))]] = FrozenMap()
    labels: tuple[str, ...] = ()
    at: TimestampValue | None = None
    history: tuple[TimestampValue, ...] = ()
    marked: FrozenMap[str, TimestampValue] = FrozenMap()

    __hash__ = CoreValue.cached_hash
