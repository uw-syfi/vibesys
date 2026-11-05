"""HDR histograms: what loadgen's summary encodes, Python reads back with HdrHistogram's precision."""

from __future__ import annotations

import base64

import pytest
from hypothesis import given
from hypothesis import strategies as st

# test-isolation: the histogram codec is private; the summary's percentiles depend on it exactly.
from vibesys_evaluator._private.hdr import Histogram, HistogramError, decode, encode

values = st.lists(st.integers(min_value=0, max_value=60_000_000), min_size=1, max_size=300)


def _histogram(recorded: list[int], figures: int = 3) -> Histogram:
    histogram = Histogram(lowest=1, highest=60_000_000, significant_figures=figures)
    for value in recorded:
        histogram.record(value)
    return histogram


@given(values, st.sampled_from([1, 2, 3, 4]))
def test_encoding_round_trips(recorded: list[int], figures: int) -> None:
    histogram = _histogram(recorded, figures)
    decoded = decode(encode(histogram))
    assert decoded.counts == histogram.counts
    assert decoded.total == len(recorded)


@given(values, st.sampled_from([50.0, 90.0, 99.0, 99.9, 100.0]))
def test_a_percentile_is_the_ranked_value_within_the_precision(
    recorded: list[int], p: float
) -> None:
    rank = max(1, int(p / 100 * len(recorded) + 0.5))
    exact = sorted(recorded)[rank - 1]
    got = _histogram(recorded).percentile(p)
    assert exact <= got <= exact + max(1, exact // 500)  # 3 significant figures


def test_the_base64_form_of_the_go_and_java_libraries_decodes() -> None:
    histogram = _histogram([1, 10, 100, 1000])
    printed = base64.b64encode(encode(histogram))
    assert printed.startswith(b"HISTF")
    assert decode(printed).counts == histogram.counts


def test_an_empty_histogram_has_percentile_zero() -> None:
    assert _histogram([]).percentile(99.0) == 0


@pytest.mark.parametrize(
    "data",
    [b"", b"\x00\x00\x00\x00\x00\x00\x00\x00", b"HISTF!!!", b"\x1c\x84\x93\x04\x00\x00\x00\x02xx"],
    ids=["empty", "bad-cookie", "bad-base64", "bad-zlib"],
)
def test_garbage_is_refused(data: bytes) -> None:
    with pytest.raises(HistogramError):
        decode(data)
