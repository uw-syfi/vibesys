"""Bounded framing parser shared by file readers and writers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import BinaryIO

import google_crc32c

from vs_proto.api import Record

MAGIC = b"VSRLOG01"
MAX_RECORD_BYTES = 1024 * 1024
_MAX_VARINT_BYTES = 10
_CONTINUATION_BIT = 128
_CHECKSUM_BYTES = 4


class StopReason(StrEnum):
    """Why a reader stopped at its next record boundary."""

    END = "end"
    TORN_TAIL = "torn_tail"
    CORRUPTION = "corruption"


@dataclass(frozen=True)
class ReadStop:
    """Stop offset is the first byte of the incomplete or corrupt frame."""

    reason: StopReason
    offset: int
    detail: str = ""


def encode_frame(record: Record) -> bytes:
    """Encode one bounded record with a Castagnoli checksum."""
    payload = record.to_binary()
    if len(payload) > MAX_RECORD_BYTES:
        message = f"record payload exceeds {MAX_RECORD_BYTES} bytes: {len(payload)}"
        raise ValueError(message)
    length = len(payload)
    prefix = bytearray()
    while length >= _CONTINUATION_BIT:
        prefix.append((length & 127) | 128)
        length >>= 7
    prefix.append(length)
    return bytes(prefix) + google_crc32c.value(payload).to_bytes(4, "little") + payload


def read_frame(stream: BinaryIO, offset: int) -> tuple[Record | None, ReadStop]:
    """Read at most one bounded frame without consuming an incomplete tail."""
    length = 0
    for index in range(_MAX_VARINT_BYTES):
        byte = stream.read(1)
        if not byte:
            reason = StopReason.END if index == 0 else StopReason.TORN_TAIL
            return None, ReadStop(reason, offset)
        length |= (byte[0] & 127) << (7 * index)
        if length > MAX_RECORD_BYTES:
            return None, ReadStop(StopReason.CORRUPTION, offset, "length exceeds record cap")
        if not byte[0] & 128:
            break
    else:
        return None, ReadStop(StopReason.CORRUPTION, offset, "invalid length varint")
    return _read_payload(stream, offset, length)


def _read_payload(stream: BinaryIO, offset: int, length: int) -> tuple[Record | None, ReadStop]:
    checksum = stream.read(_CHECKSUM_BYTES)
    payload = stream.read(length)
    if len(checksum) != _CHECKSUM_BYTES or len(payload) != length:
        return None, ReadStop(StopReason.TORN_TAIL, offset)
    if google_crc32c.value(payload) != int.from_bytes(checksum, "little"):
        return None, ReadStop(StopReason.CORRUPTION, offset, "CRC32C mismatch")
    try:
        record = Record.from_binary(payload)
    except ValueError as exc:
        return None, ReadStop(StopReason.CORRUPTION, offset, f"invalid Record: {exc}")
    return record, ReadStop(StopReason.END, stream.tell())
