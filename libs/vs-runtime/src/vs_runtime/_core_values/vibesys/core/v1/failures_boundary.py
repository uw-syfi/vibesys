# Generated from vibesys/core/v1/failures.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations

from typing import Callable, Literal, cast

from protobuf import Message, Oneof

import vs_core.api.values as values
from vs_proto.api import core_failures as pb
from vs_runtime._core_values.vibesys.core.v1.vocabulary_boundary import (
    decode_ReplyErrorCode,
    encode_ReplyErrorCode,
)
from vs_runtime._corevalue_wkt import decode_timestamp, encode_timestamp


def _required[T](value: T | None) -> T:
    if value is None:
        raise ValueError("Required protobuf message is absent")
    return value


def encode_ReplyPathPart(v: str | int) -> pb.ReplyPathPart:
    if type(v) is str:
        return pb.ReplyPathPart(value=Oneof("key", v))
    if type(v) is int:
        return pb.ReplyPathPart(value=Oneof("index", str(v)))
    raise TypeError("Invalid ReplyPathPart member: " + type(v).__name__)


def decode_ReplyPathPart(m: pb.ReplyPathPart) -> str | int:
    match m.value:
        case Oneof("key", value):
            return value
        case Oneof("index", value):
            return int(value)
        case _:
            raise ValueError("Missing ReplyPathPart member")


def encode_ReplyError(v: values.ReplyError) -> pb.ReplyError:
    return pb.ReplyError(
        path=[encode_ReplyPathPart(x) for x in v.path],
        code=encode_ReplyErrorCode(v.code),
        message=v.message,
    )


def decode_ReplyError(m: pb.ReplyError) -> values.ReplyError:
    return values.ReplyError(
        path=tuple(decode_ReplyPathPart(x) for x in m.path),
        code=decode_ReplyErrorCode(m.code),
        message=m.message,
    )


ENCODERS: dict[type, Callable[..., Message]] = {
    values.ReplyError: encode_ReplyError,
}
DECODERS: dict[type, Callable[..., object]] = {
    pb.ReplyPathPart: decode_ReplyPathPart,
    pb.ReplyError: decode_ReplyError,
}
