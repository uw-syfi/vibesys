"""Lossless conversion of protobuf well-known types at the shell boundary."""

from vs_core.api import TimestampValue
from vs_proto.api import Timestamp


def encode_timestamp(value: TimestampValue) -> Timestamp:
    """Retain all nanoseconds, including instants before the Unix epoch."""
    return Timestamp(seconds=value.seconds, nanos=value.nanos)


def decode_timestamp(value: Timestamp) -> TimestampValue:
    """Validate Timestamp's intrinsic range when building the immutable value."""
    return TimestampValue(seconds=value.seconds, nanos=value.nanos)
