"""Validate with the message's own retained schema, independent of validation order."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from protobuf import Message
from protovalidate import Validator

if TYPE_CHECKING:
    from google.protobuf.message import Message as GoogleMessage


@lru_cache(maxsize=64)
def _validator(_file: object) -> Validator:
    # Descriptor identity is the cache key; native message loading stays lazy.
    # A bounded cache retains schema versions without accumulating every version
    # a long-lived reader has encountered. Eviction only recompiles validators.
    return Validator()


def validate(message: Message | GoogleMessage, *, fail_fast: bool = False) -> None:
    """Validate this schema version; equal file/type names cannot replace its rules."""
    file = message.desc().file if isinstance(message, Message) else message.DESCRIPTOR.file
    _validator(file).validate(message, fail_fast=fail_fast)
