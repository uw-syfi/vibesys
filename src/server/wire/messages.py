"""Small helpers over the generated messages.

Generated messages are mutable. The event store and journal hand the same
``RunEvent`` objects to every reader, so code that receives one must treat it as
read-only and derive variants with :func:`replace`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

from google.protobuf.message import Message

from server.wire import PROTOCOL_VERSION
from server.wire.v2 import events_pb2, requests_pb2

if TYPE_CHECKING:
    from google.protobuf.timestamp_pb2 import Timestamp

M = TypeVar("M", bound=Message)

_DATA_FIELD_BY_TYPE = {
    field.message_type.full_name: field.name
    for field in events_pb2.RunEvent.DESCRIPTOR.oneofs_by_name["data"].fields
}


def now() -> Timestamp:
    """Return the current UTC time as a protobuf timestamp."""
    from google.protobuf.timestamp_pb2 import Timestamp  # noqa: PLC0415

    stamp = Timestamp()
    stamp.FromDatetime(datetime.now(UTC))
    return stamp


def to_datetime(stamp: Timestamp) -> datetime:
    """Convert a protobuf timestamp to an aware UTC datetime."""
    return stamp.ToDatetime(tzinfo=UTC)


def from_datetime(value: datetime) -> Timestamp:
    """Convert a datetime to a protobuf timestamp (naive values are taken as UTC)."""
    from google.protobuf.timestamp_pb2 import Timestamp  # noqa: PLC0415

    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def replace(message: M, **updates: Any) -> M:  # noqa: ANN401
    """Return a copy of ``message`` with fields overwritten.

    ``None`` clears a field. A message-valued update is copied in, so the
    result never aliases the argument.
    """
    clone = type(message)()
    clone.CopyFrom(message)
    for name, value in updates.items():
        if value is None:
            clone.ClearField(name)
        elif isinstance(value, Message):
            getattr(clone, name).CopyFrom(value)
        else:
            setattr(clone, name, value)
    return clone


def payload_of(event: events_pb2.RunEvent) -> Message | None:
    """Return the typed payload of an event, or ``None`` when it has none."""
    case = event.WhichOneof("data")
    return None if case is None else getattr(event, case)


def set_payload(event: events_pb2.RunEvent, payload: Message) -> None:
    """Set the ``data`` oneof of ``event`` to ``payload`` by its message type."""
    field = _DATA_FIELD_BY_TYPE.get(payload.DESCRIPTOR.full_name)
    if field is None:
        message = f"{payload.DESCRIPTOR.full_name} is not an event payload"
        raise TypeError(message)
    getattr(event, field).CopyFrom(payload)


def make_event(
    event_type: events_pb2.EventType.ValueType,
    text: str = "",
    *,
    data: Message | None = None,
    **fields: Any,  # noqa: ANN401
) -> events_pb2.RunEvent:
    """Create an unrecorded event stamped with the current time.

    ``fields`` are envelope fields (``status``, ``round_label``, ``diagnostic``,
    ...); ``None`` values are skipped so callers can pass optional context
    through. ``data`` is placed in the payload oneof by its message type.
    """
    event = events_pb2.RunEvent(
        protocol_version=PROTOCOL_VERSION, timestamp=now(), type=event_type, text=text
    )
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, Message):
            getattr(event, name).CopyFrom(value)
        else:
            setattr(event, name, value)
    if data is not None:
        set_payload(event, data)
    return event


def make_request(body: str, /, **fields: Any) -> requests_pb2.Request:  # noqa: ANN401
    """Create a client request with a fresh id and timestamp.

    ``body`` names the ``Request.body`` oneof member (``"snapshot"``,
    ``"steer"``, ...) and ``fields`` populate it. ``None`` values are skipped;
    message values are copied in.
    """
    import uuid  # noqa: PLC0415

    request = requests_pb2.Request(
        protocol_version=PROTOCOL_VERSION, request_id=uuid.uuid4().hex, timestamp=now()
    )
    payload = getattr(request, body)
    payload.SetInParent()
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, Message):
            getattr(payload, name).CopyFrom(value)
        else:
            setattr(payload, name, value)
    return request
