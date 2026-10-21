"""Validated boundary between pure core values and generated protobuf messages.

The legacy JSON envelope keeps its exact spelling; protobuf JSON is a transport
representation and is never substituted for an existing store.json field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from vs_core.api import TimestampValue
from vs_proto.api import Timestamp, validate
from vs_runtime._core_values.vibesys.core.v1.common_boundary import DECODERS as COMMON_DECODERS
from vs_runtime._core_values.vibesys.core.v1.common_boundary import ENCODERS as COMMON_ENCODERS
from vs_runtime._core_values.vibesys.core.v1.control_boundary import DECODERS, ENCODERS
from vs_runtime._corevalue_wkt import decode_timestamp, encode_timestamp

if TYPE_CHECKING:
    from collections.abc import Callable

    from protobuf import Message


_ENCODERS = {**COMMON_ENCODERS, **ENCODERS, TimestampValue: encode_timestamp}
_DECODERS = {**COMMON_DECODERS, **DECODERS, Timestamp: decode_timestamp}


def to_proto(value: object) -> Message:
    """Encode a supported immutable value and validate the protobuf at the boundary."""
    try:
        encode = _ENCODERS[type(value)]
    except KeyError:
        message = f"Unsupported core value: {type(value).__qualname__}"
        raise TypeError(message) from None
    result = cast("Callable[[Any], Message]", encode)(value)
    validate(result)
    return result


def from_proto(message: Message) -> object:
    """Validate a protobuf before constructing immutable values, including history rules."""
    validate(message)
    try:
        decode = _DECODERS[type(message)]
    except KeyError:
        detail = f"Unsupported core message: {type(message).__qualname__}"
        raise TypeError(detail) from None
    return cast("Callable[[Message], object]", decode)(message)


def value_types() -> tuple[type, ...]:
    """The closed set of value classes supported by this boundary."""
    return tuple(_ENCODERS)
