"""Pure support for generated immutable values and the legacy JSON envelope boundary.

The proto compiler emits validation into each value. TypeAdapter supplies the existing
strict JSON spelling while Pydantic envelopes remain in use during the migration.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import Field as DataclassField
from dataclasses import fields, replace
from functools import cache
from types import GenericAlias, MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, Self, get_args

from pydantic import ConfigDict, Field, GetCoreSchemaHandler, TypeAdapter
from pydantic.dataclasses import dataclass, is_pydantic_dataclass, rebuild_dataclass

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo
    from pydantic.json_schema import JsonSchemaValue


@cache
def adapter[T](cls: type[T]) -> TypeAdapter[T]:
    """One compiled JSON boundary per generated class."""
    return TypeAdapter(cls)


class CoreValue:
    """Frozen generated value, with a hash cache outside its fields and JSON."""

    __slots__ = ("_hash",)
    model_fields: ClassVar[tuple[str, ...]]
    __dataclass_fields__: ClassVar[dict[str, DataclassField[object]]]
    __pydantic_fields__: ClassVar[dict[str, FieldInfo]]

    @classmethod
    def __get_pydantic_core_schema__(cls, source: type, handler: GetCoreSchemaHandler) -> object:
        schema = handler(source)
        # BaseModel accepted dictionaries in strict mode. Preserve that boundary while
        # leaving strict scalar and nested-instance validation in the dataclass config.
        if schema["type"] == "dataclass":
            schema["strict"] = False

        def exact_type(value: CoreValue) -> CoreValue:
            if type(value) is not cls:
                raise ValueTypeError(cls.__name__, type(value).__name__)
            return value

        # Validate after the dataclass so JSON arrays retain JSON-mode tuple parsing.
        # Put the reference on the wrapper, so nested uses retain the nominal check too.
        inner = dict(schema)
        reference = inner.pop("ref", None)
        result = {
            "type": "function-after",
            "function": {"type": "no-info", "function": exact_type},
            "schema": inner,
        }
        if reference is not None:
            result["ref"] = reference
        return result

    def __deepcopy__(self, memo: dict[int, object] | None = None) -> Self:
        return self

    def model_dump(
        self,
        *,
        mode: Literal["python", "json"] = "python",
        exclude: set[str] | None = None,
        round_trip: bool = False,
    ) -> dict[str, Any]:
        return adapter(type(self)).dump_python(
            self, mode=mode, exclude=exclude, round_trip=round_trip
        )

    def model_dump_json(self) -> str:
        return adapter(type(self)).dump_json(self).decode()

    @classmethod
    def model_validate(cls, value: object, *, strict: bool | None = None) -> Self:
        return adapter(cls).validate_python(value, strict=strict)

    @classmethod
    def model_validate_json(cls, value: str | bytes) -> Self:
        return adapter(cls).validate_json(value)

    @classmethod
    def model_json_schema(cls) -> JsonSchemaValue:
        return adapter(cls).json_schema()

    def model_copy(self, *, update: Mapping[str, object] | None = None) -> Self:
        return replace(self, **(update or {}))

    def cached_hash(self) -> int:
        """The hash of the frozen fields, computed once."""
        try:
            return self._hash
        except AttributeError:
            value = tuple.__hash__(tuple(getattr(self, f.name) for f in fields(self)))
            object.__setattr__(self, "_hash", value)
            return value


class FrozenMap[K, V](Mapping[K, V]):
    """A copied immutable map with order-independent equality and cached hash."""

    __slots__ = ("_data", "_hash")
    __pydantic_serializer__: ClassVar[object]

    def __init__(self, items: Mapping[K, V] | None = None) -> None:
        object.__setattr__(self, "_data", MappingProxyType(dict(items or {})))

    @classmethod
    def __get_pydantic_core_schema__(cls, source: object, handler: GetCoreSchemaHandler) -> object:
        arguments = get_args(source)
        dictionary = handler.generate_schema(GenericAlias(dict, arguments))
        return {
            "type": "function-before",
            "function": {"type": "no-info", "function": _map_input},
            "schema": {
                "type": "function-after",
                "function": {"type": "no-info", "function": cls},
                "schema": dictionary,
                "serialization": {"type": "function-plain", "function": _map_output},
            },
        }

    def __getitem__(self, key: K) -> V:
        return self._data[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        try:
            return self._hash
        except AttributeError:
            value = frozenset.__hash__(frozenset(self.items()))
            object.__setattr__(self, "_hash", value)
            return value

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenMapError(name)

    def __delattr__(self, name: str) -> None:
        raise FrozenMapError(name)


class FrozenMapError(AttributeError):
    """A caller attempted to mutate an immutable map."""

    def __init__(self, name: str) -> None:
        super().__init__(f"FrozenMap is immutable: {name}")


def _map_input(value: object) -> object:
    """Retain the strict dict boundary, also accepting an already frozen map."""
    return dict(value) if isinstance(value, FrozenMap) else value


def _map_output(value: FrozenMap) -> dict:
    """The envelope's ordinary JSON object spelling."""
    return dict(value)


# Envelope canonicalization uses Pydantic's duck-typed serialization. Register the
# same map serializer there, where a field's generic annotation is unavailable.
FrozenMap.__pydantic_serializer__ = TypeAdapter(FrozenMap[object, object]).serializer


# Timestamp's intrinsic range is part of the protobuf well-known type, rather than
# an application rule. Keeping nanos avoids datetime's microsecond truncation.
@dataclass(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra="forbid", strict=True))
class TimestampValue(CoreValue):
    """Immutable nanosecond-precision google.protobuf.Timestamp."""

    model_fields: ClassVar[tuple[str, ...]] = ("seconds", "nanos")
    seconds: Annotated[int, Field(ge=-62135596800, le=253402300799)] = 0
    nanos: Annotated[int, Field(ge=0, le=999999999)] = 0

    __hash__ = CoreValue.cached_hash


def bounded_integer(value: int, *, minimum: int, maximum: int) -> int:
    """Enforce a protobuf scalar's intrinsic range without changing legacy JSON schemas."""
    if not minimum <= value <= maximum:
        raise IntegerRangeError(minimum, maximum)
    return value


class IntegerRangeError(ValueError):
    """An integer is outside the wire scalar's representable range."""

    def __init__(self, minimum: int, maximum: int) -> None:
        super().__init__(f"integer outside protobuf range [{minimum}, {maximum}]")


class ValueTypeError(ValueError):
    """A nominal value cannot substitute for another generated value's domain."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"value type mismatch: expected {expected}, got {actual}")


def omit_unset(value: object) -> bool:
    """Keep the legacy spelling of optional data absent from an envelope."""
    return value is None or value is False or (isinstance(value, tuple) and not value)


class ContractValidationError(ValueError):
    """Invalid immutable value, identifying the exact field and violated contract."""

    def __init__(self, path: str, detail: str) -> None:
        """Name the offending field without masking a plausible default."""
        super().__init__(f"{path}: {detail}")


def resolve_value(cls: type[CoreValue]) -> None:
    """Resolve a generated file's forward unions before nested serializers see its values."""
    if not is_pydantic_dataclass(cls):
        expected = "Pydantic dataclass"
        raise ValueTypeError(expected, cls.__name__)
    rebuild_dataclass(cls)


def cel_integer(value: int) -> int:
    """Preserve CEL's checked int64 conversion after a rule bounds a decimal ordinal."""
    return bounded_integer(value, minimum=-(2**63), maximum=2**63 - 1)
