from __future__ import annotations


# Generated from vibesys/core/testing/v1/values.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations
from protobuf import Message, Oneof
from typing import Callable, Literal, cast
import scripts.corevalue.tests.values_api as values
from vs_proto.api import compiler_values as pb
from vs_runtime._corevalue_wkt import encode_timestamp, decode_timestamp

def encode_Choice(v: values.Choice | None) -> pb.Choice:
    return {'first': pb.Choice.FIRST, 'second': pb.Choice.SECOND, 'third': pb.Choice.THIRD}.get(v, pb.Choice.UNSPECIFIED)

def decode_Choice(v: pb.Choice) -> values.Choice:
    return values.Choice({1: 'first', 2: 'second', 3: 'third'}[v])

def encode_LiteralChoice(v: Literal['first', 'second', 'third'] | None) -> pb.LiteralChoice:
    return {'first': pb.LiteralChoice.FIRST, 'second': pb.LiteralChoice.SECOND, 'third': pb.LiteralChoice.THIRD}.get(v, pb.LiteralChoice.UNSPECIFIED)

def decode_LiteralChoice(v: pb.LiteralChoice) -> Literal['first', 'second', 'third']:
    return cast(Literal['first', 'second', 'third'], {1: 'first', 2: 'second', 3: 'third'}[v])

def _required[T](value: T | None) -> T:
    if value is None:
        raise ValueError('Required protobuf message is absent')
    return value

def encode_MapValue(v: values.MapValue) -> pb.MapValue:
    return pb.MapValue(counts={k: v.counts[k] for k in sorted(v.counts)}, labels=[x for x in v.labels], at=None if v.at is None else encode_timestamp(v.at), history=[encode_timestamp(x) for x in v.history], marked={k: encode_timestamp(v.marked[k]) for k in sorted(v.marked)})

def decode_MapValue(m: pb.MapValue) -> values.MapValue:
    return values.MapValue(counts=values.FrozenMap({k: m.counts[k] for k in sorted(m.counts)}), labels=tuple(x for x in m.labels), at=None if m.at is None else decode_timestamp(m.at), history=tuple(decode_timestamp(x) for x in m.history), marked=values.FrozenMap({k: decode_timestamp(m.marked[k]) for k in sorted(m.marked)}))

def encode_Quantity(v: values.Quantity) -> pb.Quantity:
    return pb.Quantity(value=v.value, lower=v.lower, upper=v.upper)

def decode_Quantity(m: pb.Quantity) -> values.Quantity:
    return values.Quantity(value=m.value, lower=m.lower, upper=m.upper)

def encode_ExtendedQuantity(v: values.ExtendedQuantity) -> pb.ExtendedQuantity:
    return pb.ExtendedQuantity(value=v.value, lower=v.lower, upper=v.upper, label=v.label)

def decode_ExtendedQuantity(m: pb.ExtendedQuantity) -> values.ExtendedQuantity:
    return values.ExtendedQuantity(value=m.value, lower=m.lower, upper=m.upper, label=m.label)

def encode_Ordinal(v: values.Ordinal) -> pb.Ordinal:
    return pb.Ordinal(ordinal=str(v.ordinal))

def decode_Ordinal(m: pb.Ordinal) -> values.Ordinal:
    return values.Ordinal(ordinal=int(m.ordinal))

def encode_OrderedValues(v: values.OrderedValues) -> pb.OrderedValues:
    return pb.OrderedValues(records=[encode_Ordinal(x) for x in v.records], labels=[x for x in sorted(v.labels)], quantity=None if v.quantity is None else encode_Quantity(v.quantity), coverage=[encode_Ordinal(x) for x in v.coverage], explicit_text=v.explicit_text)

def decode_OrderedValues(m: pb.OrderedValues) -> values.OrderedValues:
    return values.OrderedValues(records=tuple(decode_Ordinal(x) for x in m.records), labels=frozenset(x for x in m.labels), quantity=None if m.quantity is None else decode_Quantity(m.quantity), coverage=tuple(decode_Ordinal(x) for x in m.coverage), explicit_text=m.explicit_text if m.has_field('explicit_text') else None)

def encode_EnumSelection(v: values.EnumSelection) -> pb.EnumSelection:
    return pb.EnumSelection(choice=encode_Choice(v.choice), literal=encode_LiteralChoice(v.literal))

def decode_EnumSelection(m: pb.EnumSelection) -> values.EnumSelection:
    return values.EnumSelection(choice=cast(Literal[values.Choice.FIRST, values.Choice.THIRD], decode_Choice(m.choice)), literal=cast(Literal['first', 'third'], decode_LiteralChoice(m.literal)))

ENCODERS: dict[type, Callable[..., Message]] = {
    values.MapValue: encode_MapValue,
    values.Quantity: encode_Quantity,
    values.ExtendedQuantity: encode_ExtendedQuantity,
    values.Ordinal: encode_Ordinal,
    values.OrderedValues: encode_OrderedValues,
    values.EnumSelection: encode_EnumSelection,
}
DECODERS: dict[type, Callable[..., object]] = {
    pb.MapValue: decode_MapValue,
    pb.Quantity: decode_Quantity,
    pb.ExtendedQuantity: decode_ExtendedQuantity,
    pb.Ordinal: decode_Ordinal,
    pb.OrderedValues: decode_OrderedValues,
    pb.EnumSelection: decode_EnumSelection,
}
