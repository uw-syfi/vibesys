"""HDR histograms in the standard compressed V2 encoding, as loadgen writes them.

The decoding, merging and percentile rule live in the core (``vibesys-evaluator hist
summary``); a ``Histogram`` here is what one call returned. A percentile is the highest value
equivalent to the first bucket whose cumulative count reaches ``int(p / 100 * total + 0.5)``.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from vibesys_evaluator._private import core

if TYPE_CHECKING:
    from collections.abc import Sequence


class HistogramError(ValueError):
    """A histogram that is not in the compressed V2 encoding."""


def summarize(
    histograms: Sequence[bytes], percentiles: Sequence[float] = ()
) -> tuple[int, int, list[int]]:
    """Merge ``histograms`` (raw bytes, or the base64 text the Go and Java libraries print).

    Returns:
        The merged count, its maximum and the value at each of ``percentiles`` (0 to 100).

    Raises:
        HistogramError: A histogram is not in the compressed V2 encoding.
    """
    request = {
        "histograms": [base64.b64encode(h).decode("ascii") for h in histograms],
        "percentiles": list(percentiles),
    }
    try:
        reply = core.call("hist", "summary", request)
    except core.CoreDomainError as error:
        if error.kind == "histogram":
            raise HistogramError(error.message) from error
        raise
    return int(reply["count"]), int(reply["max"]), [int(v) for v in reply.get("values", [])]


class Histogram:
    """A decoded histogram: its count and maximum, and percentiles read from the core."""

    def __init__(self, data: bytes, count: int, maximum: int) -> None:
        """Keep the encoded form; ``count`` and ``maximum`` are what decoding found."""
        self._data = data
        self.total = count
        self.max = maximum

    def percentile(self, p: float) -> int:
        """The value at percentile ``p`` (0 to 100); 0 for an empty histogram."""
        return summarize([self._data], [p])[2][0]


def decode(data: bytes) -> Histogram:
    """Decode a compressed V2 histogram (raw, or base64 as the Go and Java libraries print it)."""
    count, maximum, _ = summarize([data])
    return Histogram(data, count, maximum)
