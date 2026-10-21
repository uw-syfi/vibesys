from __future__ import annotations


# Generated from vibesys/core/testing/v1/values.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations
from protobuf import Message, Oneof
from typing import Callable, Literal, cast
import scripts.corevalue.tests.values_api as values
from scripts.corevalue.tests.generated_pb.vibesys.core.testing.v1 import values_pb as pb
from vs_runtime._corevalue_wkt import encode_timestamp, decode_timestamp

def _required[T](value: T | None) -> T:
    if value is None:
        raise ValueError('Required protobuf message is absent')
    return value

def encode_MapValue(v: values.MapValue) -> pb.MapValue:
    return pb.MapValue(counts={k: v.counts[k] for k in sorted(v.counts)}, labels=[x for x in v.labels], at=None if v.at is None else encode_timestamp(v.at), history=[encode_timestamp(x) for x in v.history], marked={k: encode_timestamp(v.marked[k]) for k in sorted(v.marked)})

def decode_MapValue(m: pb.MapValue) -> values.MapValue:
    return values.MapValue(counts=values.FrozenMap({k: m.counts[k] for k in sorted(m.counts)}), labels=tuple(x for x in m.labels), at=None if m.at is None else decode_timestamp(m.at), history=tuple(decode_timestamp(x) for x in m.history), marked=values.FrozenMap({k: decode_timestamp(m.marked[k]) for k in sorted(m.marked)}))

ENCODERS: dict[type, Callable[..., Message]] = {
    values.MapValue: encode_MapValue,
}
DECODERS: dict[type, Callable[..., object]] = {
    pb.MapValue: decode_MapValue,
}
