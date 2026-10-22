"""Strict temporary projections between neutral protobufs and adapter model values.

This is an in-memory projection, never a JSON wire contract. Enum spellings and
union tags come from neutral descriptors, so adapters declare no second schema.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from protobuf import (
    DescEnum,
    DescEnumValue,
    DescFieldValueEnum,
    DescFieldValueList,
    DescFieldValueMap,
    DescFieldValueMessage,
    DescMessage,
    Enum,
    Message,
    Oneof,
)

from vs_proto._generated.buf.validate import validate_pb as rules
from vs_proto._generated.vibesys.host.v1 import operation_options_pb as options

if TYPE_CHECKING:
    from protobuf import DescField


def _spelling(value: DescEnumValue) -> str:
    opts = value.proto.options
    if opts is None or options.ext_adapter_spelling not in opts:
        message = f"{value.name}: missing adapter spelling"
        raise ValueError(message)
    return opts[options.ext_adapter_spelling]


def _element(kind: object, value: object) -> object:
    if isinstance(kind, DescEnum):
        found = next(
            (item for item in kind.values if item.number and _spelling(item) == value), None
        )
        if found is None:
            message = f"{kind.type_name}: unknown enum spelling {value!r}"
            raise ValueError(message)
        return kind.type(found.number)
    if isinstance(kind, DescMessage):
        if not isinstance(value, Mapping):
            message = f"{kind.type_name}: expected mapping"
            raise TypeError(message)
        return from_mapping(kind.type, value)
    return value


def _field(field: DescField, value: object) -> object:
    match field.value:
        case DescFieldValueMessage(message=kind):
            return _message_field(kind, value)
        case DescFieldValueEnum(enum=kind):
            return _element(kind, value)
        case DescFieldValueList(element=kind):
            if not isinstance(value, list | tuple):
                message = f"{field.name}: expected sequence"
                raise TypeError(message)
            return [_element(kind, item) for item in value]
        case DescFieldValueMap(value=kind):
            if not isinstance(value, Mapping):
                message = f"{field.name}: expected mapping"
                raise TypeError(message)
            return {key: _element(kind, item) for key, item in value.items()}
        case _:
            return value


def _message_field(kind: DescMessage, value: object) -> object:
    if len(kind.fields) == 1 and kind.fields[0].name == "items":
        return kind.type(items=_field(kind.fields[0], value))
    if kind.oneofs:
        if not isinstance(value, Mapping):
            message = f"{kind.type_name}: expected union mapping"
            raise TypeError(message)
        return _union(kind, value)
    return _element(kind, value)


def _tag_constant(field: DescField) -> str | None:
    opts = field.proto.options
    rule = opts[rules.ext_field] if opts is not None and rules.ext_field in opts else None
    if rule is not None:
        match rule.type:
            case Oneof("string", value):
                return value.const
    return None


def _union(kind: DescMessage, value: Mapping[str, object]) -> Message:
    for field in kind.fields:
        if not isinstance(field.value, DescFieldValueMessage):
            continue
        member = field.value.message
        tag = next((f for f in member.fields if f.name == "kind"), None)
        if tag is not None and value.get("kind") == _tag_constant(tag):
            return kind.type(value=Oneof(field.name, from_mapping(member.type, value)))
    message = f"{kind.type_name}: unknown union kind {value.get('kind')!r}"
    raise ValueError(message)


def from_mapping[Value: Message](kind: type[Value], value: Mapping[str, object]) -> Value:
    """Reject unknown keys and copy model values into their exact descriptor shape."""
    fields = {field.name: field for field in kind.desc().fields}
    unknown = value.keys() - fields.keys()
    if unknown:
        message = f"{kind.desc().type_name}: unknown keys {sorted(unknown)}"
        raise ValueError(message)
    return kind(
        **{name: _field(fields[name], item) for name, item in value.items() if item is not None}
    )


def _project(value: object) -> object:
    if isinstance(value, list):
        return [_project(item) for item in value]
    if isinstance(value, dict):
        return {key: _project(item) for key, item in value.items()}
    if isinstance(value, Enum):
        return _spelling(next(item for item in value.desc().values if item.number == int(value)))
    if isinstance(value, Message):
        return _project_message(value)
    return value


def _project_message(value: Message) -> object:
    descriptor = value.desc()
    if len(descriptor.fields) == 1 and descriptor.fields[0].name == "items":
        return _project(getattr(value, descriptor.fields[0].name))
    if descriptor.oneofs:
        variant = getattr(value, descriptor.oneofs[0].name)
        return None if variant is None else _project(variant.value)
    return to_mapping(value)


def to_mapping(value: Message) -> dict[str, object]:
    """Project all fields, preserving optional absence separately from empty values."""
    return {
        field.name: None
        if field.proto.proto3_optional and not value.has_field(field.name)
        else _project(getattr(value, field.name))
        for field in value.desc().fields
    }
