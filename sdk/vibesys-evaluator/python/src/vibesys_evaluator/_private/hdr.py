"""HDR histograms in the standard compressed V2 encoding, as loadgen writes them.

Only what the package needs: decode a histogram, read a percentile, and (for
the package's own tests and tools) record values and encode. The layout and
the percentile rule are HdrHistogram's: a percentile is the highest value
equivalent to the first bucket whose cumulative count reaches
``int(p / 100 * total + 0.5)``, as the Go library's ``ValueAtQuantile``.
"""

from __future__ import annotations

import base64
import math
import struct
import zlib
from dataclasses import dataclass, field

_V2_COOKIE = 0x1C849303
_V2_COMPRESSED_COOKIE = 0x1C849304
# cookie, counts length, normalizing offset, digits, lowest, highest, integer-to-double ratio
_HEADER = struct.Struct(">iiiiqqd")
_HEADER_SIZE = _HEADER.size
_PREFIX = struct.Struct(">ii")  # cookie, compressed length
_MAX_FIGURES = 5
_CONTINUE = 0x80  # LEB128 continuation bit
_LAST_BYTE = 8  # the ninth byte of a 64-bit LEB128 value carries 8 bits
_BASE64_PREFIX = b"HISTF"


class HistogramError(ValueError):
    """A histogram that is not in the compressed V2 encoding."""


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
            raise HistogramError(msg)
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

    @property
    def total(self) -> int:
        """The number of values recorded."""
        return sum(self.counts)

    def _index(self, value: int) -> int:
        pow2ceiling = (value | self._sub_bucket_mask).bit_length()
        bucket = pow2ceiling - self._unit_magnitude - (self._sub_bucket_half_count_magnitude + 1)
        sub_bucket = value >> (bucket + self._unit_magnitude)
        return ((bucket + 1) << self._sub_bucket_half_count_magnitude) + (
            sub_bucket - self._sub_bucket_half_count
        )

    def _value(self, index: int) -> int:
        bucket = (index >> self._sub_bucket_half_count_magnitude) - 1
        sub_bucket = (index & (self._sub_bucket_half_count - 1)) + self._sub_bucket_half_count
        if bucket < 0:
            sub_bucket -= self._sub_bucket_half_count
            bucket = 0
        return sub_bucket << (bucket + self._unit_magnitude)

    def _highest_equivalent(self, value: int) -> int:
        pow2ceiling = (value | self._sub_bucket_mask).bit_length()
        bucket = pow2ceiling - self._unit_magnitude - (self._sub_bucket_half_count_magnitude + 1)
        sub_bucket = value >> (bucket + self._unit_magnitude)
        adjusted = bucket + 1 if sub_bucket >= 2 * self._sub_bucket_half_count else bucket
        lowest = sub_bucket << (bucket + self._unit_magnitude)
        return lowest + (1 << (self._unit_magnitude + adjusted)) - 1

    def record(self, value: int, count: int = 1) -> None:
        """Record ``value`` (clamped to the trackable range) ``count`` times."""
        value = min(max(value, 0), self.highest)
        self.counts[self._index(value)] += count

    def percentile(self, p: float) -> int:
        """The value at percentile ``p`` (0 to 100); 0 for an empty histogram."""
        total = self.total
        if total == 0:
            return 0
        target = int(min(max(p, 0.0), 100.0) / 100.0 * total + 0.5)
        running = 0
        for index, count in enumerate(self.counts):
            running += count
            if running >= target:
                return self._highest_equivalent(self._value(index))
        return 0  # pragma: no cover - the running count reaches the total


def decode(data: bytes) -> Histogram:
    """Decode a compressed V2 histogram (raw, or base64 as the Go and Java libraries print it)."""
    if data.startswith(_BASE64_PREFIX):
        try:
            data = base64.b64decode(data, validate=True)
        except ValueError as error:
            msg = f"histogram: bad base64 ({error})"
            raise HistogramError(msg) from error
    if len(data) < _PREFIX.size:
        msg = "histogram: too short"
        raise HistogramError(msg)
    cookie, length = _PREFIX.unpack(data[: _PREFIX.size])
    if cookie != _V2_COMPRESSED_COOKIE:
        msg = f"histogram: cookie {cookie:#x} is not the compressed V2 cookie"
        raise HistogramError(msg)
    try:
        payload = zlib.decompress(data[_PREFIX.size : _PREFIX.size + length])
    except zlib.error as error:
        msg = f"histogram: bad compression ({error})"
        raise HistogramError(msg) from error
    if len(payload) < _HEADER_SIZE:
        msg = "histogram: header too short"
        raise HistogramError(msg)
    cookie, counts_length, _offset, digits, lowest, highest, _ratio = _HEADER.unpack(
        payload[:_HEADER_SIZE]
    )
    if cookie != _V2_COOKIE:
        msg = f"histogram: inner cookie {cookie:#x} is not the V2 cookie"
        raise HistogramError(msg)
    histogram = Histogram(lowest=lowest, highest=highest, significant_figures=digits)
    _fill(histogram.counts, _zigzag_varints(payload[_HEADER_SIZE : _HEADER_SIZE + counts_length]))
    return histogram


def _fill(counts: list[int], values: list[int]) -> None:
    """Fill ``counts`` from encoded values, where -n stands for n empty buckets."""
    index = 0
    for value in values:
        if value < 0:
            index -= value
            continue
        if index >= len(counts):
            msg = "histogram: more counts than its range holds"
            raise HistogramError(msg)
        counts[index] = value
        index += 1


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
        _V2_COOKIE,
        len(body),
        0,
        histogram.significant_figures,
        histogram.lowest,
        histogram.highest,
        1.0,
    )
    compressed = zlib.compress(header + bytes(body))
    return _PREFIX.pack(_V2_COMPRESSED_COOKIE, len(compressed)) + compressed


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


def _zigzag_varints(data: bytes) -> list[int]:
    out: list[int] = []
    position = 0
    while position < len(data):
        value = 0
        shift = 0
        for n in range(_LAST_BYTE + 1):
            if position >= len(data):
                msg = "histogram: truncated count"
                raise HistogramError(msg)
            byte = data[position]
            position += 1
            if n == _LAST_BYTE:
                value |= byte << shift
                break
            value |= (byte & 0x7F) << shift
            shift += 7
            if byte < _CONTINUE:
                break
        out.append((value >> 1) ^ -(value & 1))
    return out
