"""Validated immutable operation requests and descriptor-driven correlation."""

from __future__ import annotations

from dataclasses import dataclass

from protobuf import Message
from protovalidate import validate

from vs_proto._canonical_operation import canonical_message

MAX_OPERATION_BYTES = 1 << 20


def checked(message: Message) -> bytes:
    """Validate a boundary message and return its bounded canonical wire bytes."""
    validate(message)
    if message.to_binary() != message.to_binary(write_unknown_fields=False):
        message_text = "operation contains unknown fields"
        raise ValueError(message_text)
    payload = canonical_message(message).to_binary()
    if len(payload) > MAX_OPERATION_BYTES:
        message_text = "operation payload exceeds 1 MiB"
        raise ValueError(message_text)
    return payload


@dataclass(frozen=True)
class AcceptedRequest[Request: Message]:
    """An immutable accepted request; decoding returns an independent mutable copy."""

    operation: Operation
    payload: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.payload, bytes):
            message = "accepted payload must be immutable bytes"
            raise TypeError(message)
        if len(self.payload) > MAX_OPERATION_BYTES:
            message = "operation payload exceeds 1 MiB"
            raise ValueError(message)
        canonical = checked(self.operation.request.from_binary(self.payload))
        if canonical != self.payload:
            message = "accepted operation bytes are not canonical"
            raise ValueError(message)

    def decode(self) -> Request:
        """Decode a fresh copy so callers cannot mutate this accepted value."""
        return self.operation.request.from_binary(self.payload)


@dataclass(frozen=True)
class Operation[Request: Message, Response: Message]:
    """The generated operation pairing, policy and response identity contract."""

    name: str
    operation_id: int
    request_tag: int
    response_tag: int
    cap_ms: int
    grace_ms: int
    effect: int
    retry: int
    executor: int
    cardinality: int
    first_event_ms: int | None
    progress_ms: int | None
    request: type[Request]
    response: type[Response]
    correlations: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]

    @property
    def total_ms(self) -> int:
        """The end-to-end budget, including dial, queue and cleanup."""
        return self.cap_ms + self.grace_ms

    def accept(self, request: Request) -> AcceptedRequest[Request]:
        """Copy and validate, keeping no reference to a mutable builder."""
        if not isinstance(request, self.request):
            message = f"{self.name}: wrong request type"
            raise TypeError(message)
        return AcceptedRequest(self, checked(request))

    def request_value(self, accepted: AcceptedRequest[Request]) -> Request:
        """Check the registry identity before a handler may perform any work."""
        if accepted.operation != self:
            message = f"{self.name}: accepted request belongs to a different registry entry"
            raise ValueError(message)
        value = self.request.from_binary(accepted.payload)
        if checked(value) != accepted.payload:
            message = f"{self.name}: accepted bytes are not canonical"
            raise ValueError(message)
        return value

    def validate_response(self, request: AcceptedRequest[Request], response: Response) -> bytes:
        """Validate pairing and every declared identity correlation before delivery."""
        if request.operation != self or not isinstance(response, self.response):
            message = f"{self.name}: wrong response method"
            raise ValueError(message)
        encoded = checked(response)
        original = request.decode()
        for left, right in self.correlations:
            if _value(original, left) != _value(response, right):
                message = f"{self.name}: response correlation mismatch: {'.'.join(right)}"
                raise ValueError(message)
        return encoded


def _value(message: object, path: tuple[str, ...]) -> object:
    for name in path:
        message = getattr(message, name)
    return message
