"""Conversions between proto enums and the string vocabularies the domain uses.

Proto enum values are ``UPPER_SNAKE`` with a type prefix (``RUN_STATUS_PAUSED``)
and a zero ``*_UNSPECIFIED`` member that means "not set". Domain code names the
same members ``PAUSED`` (enum member name) or ``"paused"`` (string value), so
the mapping is mechanical: strip the prefix, then match on upper-cased name.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from google.protobuf.descriptor import EnumDescriptor
    from google.protobuf.internal.enum_type_wrapper import EnumTypeWrapper

E = TypeVar("E", bound=Enum)


def prefix(enum_type: EnumTypeWrapper | EnumDescriptor) -> str:
    """Return the value-name prefix of a proto enum, for example ``RUN_STATUS_``."""
    descriptor: Any = getattr(enum_type, "DESCRIPTOR", enum_type)
    return descriptor.values[0].name.removesuffix("UNSPECIFIED")


def _key(value: str | Enum) -> str:
    text = value.name if isinstance(value, Enum) else value
    return text.upper().replace("-", "_")


def number(enum_type: EnumTypeWrapper, value: str | Enum) -> Any:  # noqa: ANN401
    """Map a domain string or enum member to the proto enum number.

    Raises ``ValueError`` when the proto enum has no such member, which is how
    a domain vocabulary that drifted from the proto surfaces. The result is
    typed ``Any`` because the generated stubs give every enum its own value
    type, and a plain ``int`` is not assignable to any of them.
    """
    name = prefix(enum_type) + _key(value)
    try:
        return int(enum_type.Value(name))
    except ValueError:
        message = f"{name} is not a member of {enum_type.DESCRIPTOR.full_name}"
        raise ValueError(message) from None


def text(enum_type: EnumTypeWrapper, wire_number: int) -> str:
    """Return the lower-case domain string for a proto enum number.

    ``RUN_STATUS_IMPLEMENTATION_FAILED`` becomes ``"implementation_failed"``.
    The zero member maps to ``"unspecified"``; callers that treat it as absent
    check for zero first.
    """
    return enum_type.Name(wire_number).removeprefix(prefix(enum_type)).lower()


def member(domain: type[E], enum_type: EnumTypeWrapper, wire_number: int) -> E:
    """Map a proto enum number to the domain enum member with the same name."""
    suffix = enum_type.Name(wire_number).removeprefix(prefix(enum_type))
    try:
        return domain[suffix]
    except KeyError:
        message = f"{suffix} has no member in {domain.__name__}"
        raise ValueError(message) from None


def names(enum_type: EnumTypeWrapper) -> set[str]:
    """Return the member names of a proto enum without prefix or zero member."""
    stripped = prefix(enum_type)
    return {
        item.name.removeprefix(stripped) for item in enum_type.DESCRIPTOR.values if item.number != 0
    }


def optional_number(enum_type: EnumTypeWrapper, value: str | Enum | None) -> Any:  # noqa: ANN401
    """Like :func:`number` but passes ``None`` through for an absent value."""
    return None if value is None else number(enum_type, value)


def set_optional(message: Any, field: str, value: Any) -> None:  # noqa: ANN401
    """Assign a proto3-optional field, leaving it unset for ``None``."""
    if value is not None:
        setattr(message, field, value)
