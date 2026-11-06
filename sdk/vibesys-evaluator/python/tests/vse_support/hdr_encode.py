"""A test-only HDR histogram writer: records values and writes the compressed V2 encoding.

The package itself only reads histograms (the core decodes them); the fake loadgen of the tests
writes them, in the layout loadgen uses.
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass, field

# The V2 cookie bases; writers OR in 0x10 (8-byte words) and readers mask the 0xF0 bits off.
_V2_COOKIE = 0x1C849303
_V2_COMPRESSED_COOKIE = 0x1C849304
_WORD_SIZE_BITS = 0x10
# cookie, counts length, normalizing offset, digits, lowest, highest, integer-to-double ratio
_HEADER = struct.Struct(">iiiiqqd")
_HEADER_SIZE = _HEADER.size
_PREFIX = struct.Struct(">ii")  # cookie, compressed length
_MAX_FIGURES = 5
_CONTINUE = 0x80  # LEB128 continuation bit
_LAST_BYTE = 8  # the ninth byte of a 64-bit LEB128 value carries 8 bits


@dataclass
class Histogram:
    """Counts of values in HdrHistogram buckets."""

    lowest: int
    highest: int
    significant_figures: int
    counts: list[int] = field(default_factory=list)
    _unit_magnitude: int = field(init=False)
    _sub_bucket_half_count_magnitude: int = field(init=False)
    _sub_bucket_half_count: int = field(init=False)
    _sub_bucket_mask: int = field(init=False)

    def __post_init__(self) -> None:
        """Derive the bucket layout and size the counts."""
        if (
            self.lowest < 1
            or self.highest < 2 * self.lowest
            or not 1 <= self.significant_figures <= _MAX_FIGURES
        ):
            msg = f"invalid histogram range {self.lowest}..{self.highest} ({self.significant_figures} figures)"
            raise ValueError(msg)
        largest_single_unit = 2 * 10**self.significant_figures
        sub_bucket_count_magnitude = math.ceil(math.log2(largest_single_unit))
        self._sub_bucket_half_count_magnitude = max(sub_bucket_count_magnitude, 1) - 1
        self._unit_magnitude = self.lowest.bit_length() - 1
        sub_bucket_count = 1 << (self._sub_bucket_half_count_magnitude + 1)
        self._sub_bucket_half_count = sub_bucket_count // 2
        self._sub_bucket_mask = (sub_bucket_count - 1) << self._unit_magnitude
        smallest_untrackable = sub_bucket_count << self._unit_magnitude
        bucket_count = 1
        while smallest_untrackable <= self.highest:
            if smallest_untrackable > (1 << 62):
                bucket_count += 1
                break
            smallest_untrackable <<= 1
            bucket_count += 1
        length = (bucket_count + 1) * self._sub_bucket_half_count
        if len(self.counts) < length:
            self.counts.extend([0] * (length - len(self.counts)))

    def _index(self, value: int) -> int:
        pow2ceiling = (value | self._sub_bucket_mask).bit_length()
        bucket = pow2ceiling - self._unit_magnitude - (self._sub_bucket_half_count_magnitude + 1)
        sub_bucket = value >> (bucket + self._unit_magnitude)
        return ((bucket + 1) << self._sub_bucket_half_count_magnitude) + (
            sub_bucket - self._sub_bucket_half_count
        )

    def record(self, value: int, count: int = 1) -> None:
        """Record ``value`` (clamped to the trackable range) ``count`` times."""
        value = min(max(value, 0), self.highest)
        self.counts[self._index(value)] += count


def encode(histogram: Histogram) -> bytes:
    """Encode in the compressed V2 encoding (raw bytes, not base64)."""
    body = bytearray()
    zeros = 0
    last = max((i for i, c in enumerate(histogram.counts) if c), default=-1)
    for count in histogram.counts[: last + 1]:
        if count == 0:
            zeros += 1
            continue
        if zeros:
            body += _zigzag(-zeros)
            zeros = 0
        body += _zigzag(count)
    header = _HEADER.pack(
        _V2_COOKIE | _WORD_SIZE_BITS,
        len(body),
        0,
        histogram.significant_figures,
        histogram.lowest,
        histogram.highest,
        1.0,
    )
    compressed = zlib.compress(header + bytes(body))
    return _PREFIX.pack(_V2_COMPRESSED_COOKIE | _WORD_SIZE_BITS, len(compressed)) + compressed


def _zigzag(value: int) -> bytes:
    unsigned = (value << 1) ^ (value >> 63) if value < 0 else value << 1
    unsigned &= (1 << 64) - 1
    out = bytearray()
    for _ in range(_LAST_BYTE):
        if unsigned < _CONTINUE:
            out.append(unsigned)
            return bytes(out)
        out.append((unsigned & 0x7F) | 0x80)
        unsigned >>= 7
    out.append(unsigned & 0xFF)  # the ninth byte carries 8 bits
    return bytes(out)
