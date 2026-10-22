"""Reply compatibility proofs derived by the writer, never supplied by strategies."""

from __future__ import annotations

from copy import deepcopy
from functools import cache
from hashlib import sha256
from typing import TYPE_CHECKING

from protobuf import (
    DescEnum,
    DescFieldValueEnum,
    DescFieldValueList,
    DescFieldValueMap,
    DescFieldValueMessage,
    DescMessage,
)

from ._replay_identity import _frame

if TYPE_CHECKING:
    from protobuf import DescFieldValue, Message
    from protobuf.wkt.google.protobuf.descriptor_pb import DescriptorProto


def _message_schema(desc: DescMessage) -> DescriptorProto:
    schema = deepcopy(desc.proto)
    # Referenced nested declarations are included separately. Unused nested
    # types, siblings and file options cannot invalidate an unchanged consumer.
    schema.nested_type = [
        part for part in schema.nested_type if part.options is not None and part.options.map_entry
    ]
    schema.enum_type = []
    oneofs = {value.name: index for index, value in enumerate(schema.oneof_decl)}
    ordered = sorted(schema.oneof_decl, key=lambda value: value.name)
    indices = {oneofs[value.name]: index for index, value in enumerate(ordered)}
    for value in schema.field:
        if value.has_field("oneof_index"):
            value.oneof_index = indices[value.oneof_index]
    schema.oneof_decl = ordered
    schema.field.sort(key=lambda value: value.number)
    schema.extension.sort(key=lambda value: value.number)
    schema.reserved_name.sort()
    schema.reserved_range.sort(key=lambda value: (value.start, value.end))
    schema.extension_range.sort(key=lambda value: (value.start, value.end))
    return schema


def _reference(value: DescFieldValue) -> DescMessage | DescEnum | None:
    if isinstance(value, (DescFieldValueList, DescFieldValueMap)):
        referenced = value.element if isinstance(value, DescFieldValueList) else value.value
        return referenced if isinstance(referenced, (DescMessage, DescEnum)) else None
    if isinstance(value, DescFieldValueMessage):
        return value.message
    if isinstance(value, DescFieldValueEnum):
        return value.enum
    return None


def _context(desc: DescMessage | DescEnum) -> bytes:
    options = desc.file.proto.options
    features = options.features if options is not None else None
    return (
        _frame(desc.file.proto.syntax.encode())
        + _frame(str(int(desc.file.proto.edition)).encode("ascii"))
        + _frame(features.to_binary() if features is not None else b"")
    )


@cache
def reply_schema_digest(message_type: type[Message]) -> bytes:
    """Pin the reachable reply contract, excluding source comments and unrelated types.

    Equal names with different validators, defaults, field types or reachable
    declarations are incompatible. Adding fields requires explicit historical
    absent-field semantics before the consumer can accept them.
    """
    pending = [message_type.desc()]
    declarations: dict[str, bytes] = {}
    while pending:
        desc = pending.pop()
        if desc.type_name in declarations:
            continue
        declarations[desc.type_name] = _context(desc) + _frame(_message_schema(desc).to_binary())
        for field in desc.fields:
            dependency = _reference(field.value)
            if isinstance(dependency, DescMessage):
                pending.append(dependency)
            elif isinstance(dependency, DescEnum):
                declarations[dependency.type_name] = _context(dependency) + _frame(
                    dependency.proto.to_binary()
                )
    return sha256(
        b"vibesys.reply.schema.v1"
        + b"".join(
            _frame(name.encode()) + _frame(value) for name, value in sorted(declarations.items())
        )
    ).digest()
