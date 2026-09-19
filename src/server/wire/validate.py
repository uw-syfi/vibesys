"""Semantic validation that the proto schema cannot express.

Pydantic used to enforce ``ge``/``le``/``min_length``, ``FiniteFloat``, and the
required discriminator. Protobuf types give the non-negative integer bounds for
free (``uint32``); everything else lives here and runs at the trust boundaries:
requests from the socket, events read from disk, and responses before they are
sent.

The generic walk enforces two rules on every message: a singular enum must not
be ``*_UNSPECIFIED`` (absent optional enums are fine), and a ``double`` must be
finite. Request-specific rules follow.
"""

from __future__ import annotations

import functools
import math
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from google.protobuf.descriptor import FieldDescriptor

from server.wire import PROTOCOL_VERSION
from server.wire.codec import WireError

if TYPE_CHECKING:
    from google.protobuf.descriptor import Descriptor
    from google.protobuf.message import Message

    from server.wire.v2 import events_pb2, requests_pb2

MAX_EVENTS_TIMEOUT_MS = 30_000
"""Upper bound of a long-poll ``events`` query, in milliseconds."""

_ENUM = FieldDescriptor.TYPE_ENUM
_DOUBLE = FieldDescriptor.TYPE_DOUBLE
_MESSAGE = FieldDescriptor.TYPE_MESSAGE


@functools.cache
def _plan(descriptor: Descriptor) -> tuple[tuple[FieldDescriptor, str], ...]:
    """Fields that need a check, with what to check, cached per message type."""
    plan: list[tuple[FieldDescriptor, str]] = []
    for field in descriptor.fields:
        if field.type == _ENUM:
            plan.append((field, "enum"))
        elif field.type == _DOUBLE:
            plan.append((field, "double"))
        elif field.type == _MESSAGE and not field.message_type.full_name.startswith(
            "google.protobuf."
        ):
            plan.append((field, "message"))
    return tuple(plan)


def validate_message(message: Message, path: str = "") -> None:
    """Check every enum and double reachable from ``message``.

    Raises :class:`WireError` (``invalid_message``) naming the offending field.
    """
    for field, kind in _plan(message.DESCRIPTOR):
        where = f"{path}{field.name}"
        if field.is_repeated:
            _validate_repeated(message, field, kind, where)
        elif kind == "message":
            if message.HasField(field.name):
                validate_message(getattr(message, field.name), f"{where}.")
        elif not field.has_presence or message.HasField(field.name):
            _validate_scalar(getattr(message, field.name), kind, where)


def _validate_repeated(message: Message, field: FieldDescriptor, kind: str, where: str) -> None:
    for index, item in enumerate(getattr(message, field.name)):
        if kind == "message":
            validate_message(item, f"{where}[{index}].")
        else:
            _validate_scalar(item, kind, f"{where}[{index}]")


def _validate_scalar(value: float, kind: str, where: str) -> None:
    if kind == "enum" and value == 0:
        raise WireError("invalid_message", f"{where} must be set to a specific value")
    if kind == "double" and not math.isfinite(value):
        raise WireError("invalid_message", f"{where} must be a finite number")


def validate_event(event: events_pb2.RunEvent) -> None:
    """Validate one run event against the contract."""
    if event.protocol_version != PROTOCOL_VERSION:
        raise WireError(
            "protocol_version_unsupported",
            f"event protocol_version {event.protocol_version} is not {PROTOCOL_VERSION}",
        )
    if not event.HasField("timestamp"):
        raise WireError("invalid_message", "event timestamp is required")
    validate_message(event)


def validate_request(request: requests_pb2.Request) -> None:
    """Validate a client request: version, exactly one body, and field bounds."""
    if request.protocol_version != PROTOCOL_VERSION:
        raise WireError(
            "protocol_version_unsupported",
            f"This server speaks protocol version {PROTOCOL_VERSION};"
            f" the client sent {request.protocol_version}. Upgrade the client.",
        )
    body = request.WhichOneof("body")
    if body is None:
        raise WireError("invalid_message", "request must set exactly one body")
    validate_message(request)
    payload = getattr(request, body)
    if body == "steer" and not payload.text:
        raise WireError("invalid_message", "steer.text must not be empty")
    if body == "events":
        if payload.timeout_ms > MAX_EVENTS_TIMEOUT_MS:
            raise WireError(
                "invalid_message", f"events.timeout_ms must be at most {MAX_EVENTS_TIMEOUT_MS}"
            )
        _require_min_one(payload, "before_sequence", "events.before_sequence")
    if body == "subscribe":
        _require_min_one(payload, "tail", "subscribe.tail")


def _require_min_one(payload: Message, field: str, where: str) -> None:
    if payload.HasField(field) and getattr(payload, field) < 1:
        raise WireError("invalid_message", f"{where} must be at least 1")


def fill_request_defaults(request: requests_pb2.Request) -> None:
    """Give a request without an id or timestamp the defaults v1 generated."""
    if not request.request_id:
        request.request_id = uuid.uuid4().hex
    if not request.HasField("timestamp"):
        request.timestamp.FromDatetime(datetime.now(UTC))
