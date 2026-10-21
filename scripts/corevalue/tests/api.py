"""The compiler fixture's real generated types, used through their boundary converters."""

from scripts.corevalue.tests.generated_boundary.vibesys.core.testing.v1.values_boundary import (
    decode_MapValue as decode,
)
from scripts.corevalue.tests.generated_boundary.vibesys.core.testing.v1.values_boundary import (
    encode_MapValue as encode,
)
from scripts.corevalue.tests.values_api import FrozenMap, MapValue, TimestampValue

__all__ = ["FrozenMap", "MapValue", "TimestampValue", "decode", "encode"]
