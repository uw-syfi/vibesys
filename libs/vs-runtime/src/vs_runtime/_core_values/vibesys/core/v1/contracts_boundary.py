# Generated from vibesys/core/v1/contracts.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations

from typing import Callable, Literal, cast

from protobuf import Message, Oneof

import vs_core.api.values as values
from vs_proto.api import core_contracts as pb
from vs_runtime._core_values.vibesys.core.v1.common_boundary import (
    decode_SchemaRef,
    encode_SchemaRef,
)
from vs_runtime._core_values.vibesys.core.v1.vocabulary_boundary import (
    decode_LifecycleClass,
    encode_LifecycleClass,
)
from vs_runtime._corevalue_wkt import decode_timestamp, encode_timestamp


def _required[T](value: T | None) -> T:
    if value is None:
        raise ValueError("Required protobuf message is absent")
    return value


def encode_NullValue(v: None) -> pb.NullValue:
    return pb.NullValue()


def decode_NullValue(m: pb.NullValue) -> None:
    return None


def encode_PureOptionValue(v: str | int | float | bool | None) -> pb.PureOptionValue:
    if type(v) is str:
        return pb.PureOptionValue(value=Oneof("text", v))
    if type(v) is int:
        return pb.PureOptionValue(value=Oneof("integer", str(v)))
    if type(v) is float:
        return pb.PureOptionValue(value=Oneof("real", v))
    if type(v) is bool:
        return pb.PureOptionValue(value=Oneof("flag", v))
    if type(v) is type(None):
        return pb.PureOptionValue(value=Oneof("null", encode_NullValue(v)))
    raise TypeError("Invalid PureOptionValue member: " + type(v).__name__)


def decode_PureOptionValue(m: pb.PureOptionValue) -> str | int | float | bool | None:
    match m.value:
        case Oneof("text", value):
            return value
        case Oneof("integer", value):
            return int(value)
        case Oneof("real", value):
            return value
        case Oneof("flag", value):
            return value
        case Oneof("null", value):
            return decode_NullValue(value)
        case _:
            raise ValueError("Missing PureOptionValue member")


def encode_PureOption(v: values.PureOption) -> pb.PureOption:
    return pb.PureOption(key=v.key, value=encode_PureOptionValue(v.value))


def decode_PureOption(m: pb.PureOption) -> values.PureOption:
    return values.PureOption(key=m.key, value=decode_PureOptionValue(_required(m.value)))


def encode_OperationSchemaRef(v: values.OperationSchemaRef) -> pb.OperationSchemaRef:
    return pb.OperationSchemaRef(
        kind=v.kind,
        request_schema=encode_SchemaRef(v.request_schema),
        outcome_schema=encode_SchemaRef(v.outcome_schema),
        lifecycle=encode_LifecycleClass(v.lifecycle),
    )


def decode_OperationSchemaRef(m: pb.OperationSchemaRef) -> values.OperationSchemaRef:
    return values.OperationSchemaRef(
        kind=m.kind,
        request_schema=decode_SchemaRef(_required(m.request_schema)),
        outcome_schema=decode_SchemaRef(_required(m.outcome_schema)),
        lifecycle=decode_LifecycleClass(m.lifecycle),
    )


ENCODERS: dict[type, Callable[..., Message]] = {
    values.PureOption: encode_PureOption,
    values.OperationSchemaRef: encode_OperationSchemaRef,
}
DECODERS: dict[type, Callable[..., object]] = {
    pb.NullValue: decode_NullValue,
    pb.PureOptionValue: decode_PureOptionValue,
    pb.PureOption: decode_PureOption,
    pb.OperationSchemaRef: decode_OperationSchemaRef,
}
