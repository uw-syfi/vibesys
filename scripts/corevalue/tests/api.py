"""The compiler fixture's real generated types, used through their boundary converters."""

from protobuf import Message

from scripts.corevalue.tests.generated_boundary.vibesys.core.testing.v1.values_boundary import (
    DECODERS,
    ENCODERS,
)
from scripts.corevalue.tests.generated_boundary.vibesys.core.testing.v1.values_boundary import (
    decode_MapValue as decode,
)
from scripts.corevalue.tests.generated_boundary.vibesys.core.testing.v1.values_boundary import (
    encode_MapValue as encode,
)
from scripts.corevalue.tests.values_api import (
    Choice,
    EnumSelection,
    ExtendedQuantity,
    FrozenMap,
    MapValue,
    OrderedValues,
    Ordinal,
    Quantity,
    TimestampValue,
)
from vs_proto.api import compiler_values, validate

__all__ = [
    "Choice",
    "EnumSelection",
    "ExtendedQuantity",
    "FrozenMap",
    "MapValue",
    "OrderedValues",
    "Ordinal",
    "Quantity",
    "TimestampValue",
    "compiler_values",
    "decode",
    "decode_value",
    "encode",
    "encode_value",
]


def encode_value(value: object) -> Message:
    """Exercise the same validated encoder boundary as production with generic fixtures."""
    message = ENCODERS[type(value)](value)
    validate(message)
    return message


def decode_value(message: Message) -> object:
    """Validate before invoking the generated fixture decoder."""
    validate(message)
    return DECODERS[type(message)](message)
