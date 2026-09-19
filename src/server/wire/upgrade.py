"""Upgrade version 1 event records to the version 2 wire form.

Version 1 was the Pydantic contract: ``type``/``status`` and every enum were
lower-case strings, the payload was ``data`` with a ``kind`` discriminator, and
``invocation_id`` mirrored ``execution_id``. Version 2 is proto3 JSON: enums are
prefixed names, the payload is one typed key of the ``data`` oneof, and only
``execution_id`` remains.

The upgrade is driven by the proto descriptors, so a new enum or payload needs
no code here. It also adapts the core's presentation-neutral payloads, whose
``model_dump`` output has the version 1 shape, to wire messages.
"""

from __future__ import annotations

from typing import Any

from google.protobuf.descriptor import FieldDescriptor

from server.wire import PROTOCOL_VERSION, descriptors, enums
from server.wire.codec import WireError, from_dict
from server.wire.v2 import events_pb2

_DATA_FIELDS = {
    field.name: field for field in events_pb2.RunEvent.DESCRIPTOR.oneofs_by_name["data"].fields
}
_WELL_KNOWN = "google.protobuf."


def is_legacy(record: dict[str, Any]) -> bool:
    """Whether a journal record was written before the protobuf wire form."""
    return record.get("protocol_version", 1) == 1


def upgrade_event(record: dict[str, Any]) -> dict[str, Any]:
    """Return the version 2 JSON object for a version 1 event record."""
    upgraded = _convert(events_pb2.RunEvent.DESCRIPTOR, _without(record, "data", "invocation_id"))
    upgraded["protocol_version"] = PROTOCOL_VERSION
    # v1 mirrored the two ids in both directions; v2 keeps the canonical one.
    execution_id = record.get("execution_id") or record.get("invocation_id")
    if execution_id is not None:
        upgraded["execution_id"] = execution_id
    data = record.get("data")
    if data is not None:
        upgraded.update(upgrade_payload(data))
    return upgraded


def upgrade_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Map a ``{"kind": ..., **fields}`` payload to its ``{field: body}`` oneof entry."""
    kind = data.get("kind")
    field = _DATA_FIELDS.get(kind) if isinstance(kind, str) else None
    if field is None:
        raise WireError("invalid_message", f"unknown event payload kind {kind!r}")
    body = _without(data, "kind")
    if kind == "tool_result":
        body = _tool_result_body(body)
    return {field.name: _convert(descriptors.message_type(field), body)}


def event_from_legacy(record: dict[str, Any]) -> events_pb2.RunEvent:
    """Parse a version 1 record into a version 2 message."""
    return from_dict(events_pb2.RunEvent, upgrade_event(record))


def _tool_result_body(body: dict[str, Any]) -> dict[str, Any]:
    payload = body.pop("payload", None)
    if payload is None:
        return body
    kind = payload.get("kind")
    if kind == "command":
        body["command"] = _without(payload, "kind")
    elif kind == "json":
        body["json"] = {"value": payload.get("value")}
    else:
        raise WireError("invalid_message", f"unknown tool result payload kind {kind!r}")
    return body


def _without(record: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in keys}


def _convert(descriptor: Any, record: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401
    """Drop nulls and rewrite enum strings to prefixed names, recursively."""
    converted: dict[str, Any] = {}
    for key, value in record.items():
        if value is None:
            continue
        field = descriptor.fields_by_name.get(key)
        if field is None and key == "kind":
            continue  # a v1 discriminator constant on a nested payload
        converted[key] = value if field is None else _convert_field(field, value)
    return converted


def _convert_field(field: FieldDescriptor, value: Any) -> Any:  # noqa: ANN401
    if field.is_repeated and isinstance(value, list):
        return [_convert_one(field, item) for item in value]
    return _convert_one(field, value)


def _convert_one(field: FieldDescriptor, value: Any) -> Any:  # noqa: ANN401
    if field.type == FieldDescriptor.TYPE_ENUM and isinstance(value, str):
        enum_type = descriptors.enum_type(field)
        name = enums.prefix(enum_type) + value.upper().replace("-", "_")
        if name not in enum_type.values_by_name:
            raise WireError("invalid_message", f"{value!r} is not a {enum_type.name}")
        return name
    if (
        field.type == FieldDescriptor.TYPE_MESSAGE
        and not descriptors.message_type(field).full_name.startswith(_WELL_KNOWN)
        and isinstance(value, dict)
    ):
        return _convert(descriptors.message_type(field), value)
    return value
