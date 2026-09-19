"""Narrowing helpers for protobuf descriptors.

The generated stubs type ``FieldDescriptor.message_type`` and ``enum_type`` as
optional because they are unset for scalar fields, and type descriptors as a
union of the pure-Python and upb classes, so ``field`` is left untyped. Callers that have already
checked the field kind use these to get the descriptor without a cast.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from google.protobuf.descriptor import Descriptor, EnumDescriptor


def message_type(field: Any) -> Descriptor:  # noqa: ANN401
    """Return the message descriptor of a message-typed field."""
    descriptor = field.message_type
    if descriptor is None:
        message = f"{field.full_name} is not a message field"
        raise TypeError(message)
    return descriptor


def enum_type(field: Any) -> EnumDescriptor:  # noqa: ANN401
    """Return the enum descriptor of an enum-typed field."""
    descriptor = field.enum_type
    if descriptor is None:
        message = f"{field.full_name} is not an enum field"
        raise TypeError(message)
    return descriptor
