from __future__ import annotations


# Generated from vibesys/core/testing/v1/values.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations

from typing import Annotated, ClassVar, Literal, final
from enum import StrEnum
from pydantic import AfterValidator, ConfigDict, Field, field_serializer
from functools import partial
from pydantic.dataclasses import dataclass
from vs_core._corevalue import CoreValue, FrozenMap, TimestampValue, bounded_integer, omit_unset, resolve_value, cel_integer


class Choice(StrEnum):
    'Immutable Choice value.'
    FIRST = 'first'
    SECOND = 'second'
    THIRD = 'third'

LiteralChoice = Literal['first', 'second', 'third']

type Count = Annotated[int, Field(ge=1)]

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

@dataclass(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra='forbid', strict=True, validate_default=True, revalidate_instances='subclass-instances'))
class Quantity(CoreValue):
    'Immutable Quantity value.'
    model_fields: ClassVar[tuple[str, ...]] = ('value', 'lower', 'upper')
    value: Annotated[float, Field(allow_inf_nan=False)]
    lower: Annotated[float, Field(allow_inf_nan=False)]
    upper: Annotated[float, Field(allow_inf_nan=False)]

    def __post_init__(self) -> None:
        """Enforce the proto message rules at construction as well as decode."""
        if not (self.lower <= self.value  and  self.value <= self.upper):
            raise ValueError('value outside range')

    __hash__ = CoreValue.cached_hash

@final
@dataclass(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra='forbid', strict=True, validate_default=True))
class ExtendedQuantity(Quantity):
    'Immutable ExtendedQuantity value.'
    model_fields: ClassVar[tuple[str, ...]] = ('value', 'lower', 'upper', 'label')
    value: Annotated[float, Field(allow_inf_nan=False)]
    lower: Annotated[float, Field(allow_inf_nan=False)]
    upper: Annotated[float, Field(allow_inf_nan=False)]
    label: str = ''

    def __post_init__(self) -> None:
        """Enforce the proto message rules at construction as well as decode."""
        if not (self.lower <= self.value  and  self.value <= self.upper):
            raise ValueError('value outside range')

    __hash__ = CoreValue.cached_hash

@final
@dataclass(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra='forbid', strict=True, validate_default=True))
class Ordinal(CoreValue):
    'Immutable Ordinal value.'
    model_fields: ClassVar[tuple[str, ...]] = ('ordinal',)
    ordinal: Annotated[int, Field(ge=1)] = 1

    __hash__ = CoreValue.cached_hash

@final
@dataclass(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra='forbid', strict=True, validate_default=True))
class OrderedValues(CoreValue):
    'Immutable OrderedValues value.'
    model_fields: ClassVar[tuple[str, ...]] = ('records', 'labels', 'quantity', 'coverage', 'explicit_text')
    records: tuple[Ordinal, ...] = ()
    labels: frozenset[str] = frozenset()
    quantity: Quantity | None = None
    coverage: tuple[Ordinal, ...] = ()
    explicit_text: str | None

    @field_serializer('labels', when_used='json')
    def _sorted_labels(self, value: frozenset) -> list:
        """Canonical ordering for an immutable set."""
        return sorted(value)

    def __post_init__(self) -> None:
        """Enforce the proto message rules at construction as well as decode."""
        if not (all((len(str(r.ordinal)) < len(str(len(self.coverage)))  or  (len(str(r.ordinal)) == len(str(len(self.coverage)))  and  str(r.ordinal) <= str(len(self.coverage)))) for r in self.records)  and  all((self.coverage[cel_integer(r.ordinal) - 1] == r) for r in self.records)):
            raise ValueError('ordinal outside coverage')
        if not (self.records == tuple(s for s in self.coverage if (any((r == s) for r in self.records)))):
            raise ValueError('records out of order')
        if not ((len(set(p.ordinal for p in self.records)) == len(self.records))):
            raise ValueError('duplicate ordinal')

    __hash__ = CoreValue.cached_hash

@final
@dataclass(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra='forbid', strict=True, validate_default=True))
class EnumSelection(CoreValue):
    'Immutable EnumSelection value.'
    model_fields: ClassVar[tuple[str, ...]] = ('choice', 'literal')
    choice: Literal[Choice.FIRST, Choice.THIRD]
    literal: Literal['first', 'third']

    __hash__ = CoreValue.cached_hash

resolve_value(MapValue)
resolve_value(Quantity)
resolve_value(ExtendedQuantity)
resolve_value(Ordinal)
resolve_value(OrderedValues)
resolve_value(EnumSelection)
