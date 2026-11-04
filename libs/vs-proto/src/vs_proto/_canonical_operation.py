"""Canonical known-field operation bytes, including deterministically ordered maps."""

from __future__ import annotations

from typing import TYPE_CHECKING

from protobuf import (
    DescFieldValueList,
    DescFieldValueMap,
    DescFieldValueMessage,
    DescMessage,
    Message,
    Oneof,
)

if TYPE_CHECKING:
    from protobuf import DescField


def _value(field: DescField, value: object) -> object:
    match field.value:
        case DescFieldValueMessage():
            if value is not None and not isinstance(value, Message):
                message = f"{field.name}: expected message"
                raise TypeError(message)
            return canonical_message(value) if value is not None else None
        case DescFieldValueMap(value=kind):
            if not isinstance(value, dict):
                message = f"{field.name}: expected map"
                raise TypeError(message)
            return {
                key: canonical_message(value[key]) if isinstance(kind, DescMessage) else value[key]
                for key in sorted(value)
            }
        case DescFieldValueList(element=kind) if isinstance(kind, DescMessage):
            if not isinstance(value, list):
                message = f"{field.name}: expected sequence"
                raise TypeError(message)
            return [canonical_message(item) for item in value]
        case _:
            return value


def canonical_message[Value: Message](value: Value) -> Value:
    """Own a copy, retaining field presence and semantic repeated-field order."""
    result = type(value).from_binary(value.to_binary())
    for field in result.desc().fields:
        oneof = field.value.oneof if isinstance(field.value, DescFieldValueMessage) else None
        if oneof is not None:
            selected = getattr(result, oneof.name)
            if selected is not None and selected.field == field.name:
                setattr(result, oneof.name, Oneof(field.name, _value(field, selected.value)))
        elif isinstance(
            field.value, DescFieldValueMessage | DescFieldValueMap | DescFieldValueList
        ):
            setattr(result, field.name, _value(field, getattr(result, field.name)))
    return result
