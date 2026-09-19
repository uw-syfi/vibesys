"""Strict JSON encoding and decoding of the generated protocol messages.

The wire form is proto3 canonical JSON with two deliberate options: field
names are the ``snake_case`` proto names (``preserving_proto_field_name``), and
zero-valued implicit fields are omitted. Parsing rejects unknown fields, which
keeps the old "an unknown field is a capability probe" behavior: a server that
predates a field answers a request carrying it with ``invalid_message``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, TypeVar

from google.protobuf import json_format
from google.protobuf.message import Message

from server.wire import PROTOCOL_VERSION

if TYPE_CHECKING:
    from server.wire.v2 import requests_pb2

M = TypeVar("M", bound=Message)


class WireError(ValueError):
    """A message that cannot be accepted, with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:  # noqa: D107
        super().__init__(message)
        self.code = code
        self.message = message


def to_dict(message: Message) -> dict[str, Any]:
    """Return the canonical JSON object for a message."""
    return json_format.MessageToDict(message, preserving_proto_field_name=True)


def dumps(message: Message) -> str:
    """Serialize a message as compact canonical JSON on one line."""
    return json.dumps(to_dict(message), separators=(",", ":"), ensure_ascii=False)


def from_dict(cls: type[M], record: dict[str, Any]) -> M:
    """Build a message from a JSON object, rejecting unknown fields and enum names."""
    try:
        return json_format.ParseDict(record, cls(), ignore_unknown_fields=False)
    except json_format.ParseError as error:
        raise WireError("invalid_message", str(error)) from error


def loads(cls: type[M], data: str | bytes) -> M:
    """Parse one JSON document into a message strictly."""
    return from_dict(cls, _load_object(data))


def _load_object(data: str | bytes) -> dict[str, Any]:
    try:
        record = json.loads(data)
    except ValueError as error:
        raise WireError("invalid_json", f"Message is not valid JSON: {error}") from error
    if not isinstance(record, dict):
        raise WireError("invalid_message", "Message must be a JSON object")
    return record


def parse_request(data: str | bytes) -> requests_pb2.Request:
    """Decode and validate one client request line.

    A version 1 request (string ``type`` discriminator, or ``protocol_version``
    of 1) is rejected with ``protocol_version_unsupported`` and a message that
    names the fix, rather than the opaque unknown-field error strict parsing
    would give. A blank ``request_id`` or missing ``timestamp`` is filled in,
    as the v1 defaults did.
    """
    from server.wire import validate  # noqa: PLC0415  # avoids an import cycle
    from server.wire.v2 import requests_pb2  # noqa: PLC0415

    record = _load_object(data)
    version = record.get("protocol_version")
    if version != PROTOCOL_VERSION and (version == 1 or "type" in record):
        raise WireError(
            "protocol_version_unsupported",
            f"This server speaks protocol version {PROTOCOL_VERSION}; the client sent"
            f" version {version if version is not None else 1}. Upgrade the client.",
        )
    request = from_dict(requests_pb2.Request, record)
    validate.validate_request(request)
    validate.fill_request_defaults(request)
    return request
