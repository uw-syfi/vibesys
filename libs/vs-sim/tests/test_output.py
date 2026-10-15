"""The output write side: every OutputSink passes the contract, and each keeps its promise."""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import DISCARD, Capture, Lines, OutputStream, tee
from vs_sim.api.testing import OutputSinkContract, OutputSinkUnderTest

_OUTPUT = st.binary(max_size=200) | st.lists(
    st.sampled_from([b"a", b"\n", b"\r", b"\xc3\xa9", b"\xe2\x82", b"\xff"]), max_size=60
).map(b"".join)
"""Bytes in general, and bytes rich in line endings and split UTF-8 sequences."""


@st.composite
def _chunked(draw: st.DrawFn, data: bytes) -> list[bytes]:
    cuts = sorted(draw(st.lists(st.integers(0, len(data)), max_size=8)))
    bounds = [0, *cuts, len(data)]
    return [data[start:end] for start, end in itertools.pairwise(bounds)]


def _streams() -> st.SearchStrategy[dict[OutputStream, bytes]]:
    return st.fixed_dictionaries(dict.fromkeys(OutputStream, _OUTPUT))


@st.composite
def _schedule(
    draw: st.DrawFn, written: dict[OutputStream, bytes]
) -> list[tuple[OutputStream, bytes]]:
    """The streams' bytes in arbitrary chunks, arbitrarily interleaved."""
    pending = {stream: draw(_chunked(data)) for stream, data in written.items()}
    schedule: list[tuple[OutputStream, bytes]] = []
    while any(pending.values()):
        stream = draw(st.sampled_from([s for s, chunks in pending.items() if chunks]))
        schedule.append((stream, pending[stream].pop(0)))
    return schedule


def _expected_lines(data: bytes, max_line_bytes: int) -> list[str]:
    """The lines a whole stream holds, by an independent reading of the Lines contract."""
    parts = data.split(b"\n")
    if parts[-1] == b"":
        parts.pop()
    return [
        part[:max_line_bytes].removesuffix(b"\r").decode("utf-8", errors="replace")
        for part in parts
    ]


class _CollectedLines:
    def __init__(self, *, max_line_bytes: int = 64) -> None:
        self.lines: dict[OutputStream, list[str]] = {stream: [] for stream in OutputStream}
        self.sink = Lines(self.add, max_line_bytes=max_line_bytes)

    def add(self, stream: OutputStream, line: str) -> None:
        self.lines[stream].append(line)

    def observe(self) -> dict[OutputStream, list[str]]:
        self.sink.finish()
        return self.lines


def _captured(capture: Capture) -> tuple[object, ...]:
    return tuple((capture.kept(s), capture.omitted(s)) for s in OutputStream)


class TestDiscard(OutputSinkContract):
    def sink_under_test(self) -> OutputSinkUnderTest:
        return OutputSinkUnderTest(DISCARD, lambda: None)


class TestCapture(OutputSinkContract):
    def sink_under_test(self) -> OutputSinkUnderTest:
        capture = Capture(16)
        return OutputSinkUnderTest(capture, lambda: _captured(capture))


class TestLines(OutputSinkContract):
    def sink_under_test(self) -> OutputSinkUnderTest:
        collected = _CollectedLines()
        return OutputSinkUnderTest(collected.sink, collected.observe)


class TestLinesCutShort(OutputSinkContract):
    def sink_under_test(self) -> OutputSinkUnderTest:
        collected = _CollectedLines(max_line_bytes=3)
        return OutputSinkUnderTest(collected.sink, collected.observe)


class TestTee(OutputSinkContract):
    def sink_under_test(self) -> OutputSinkUnderTest:
        capture = Capture(1024)
        collected = _CollectedLines()
        return OutputSinkUnderTest(
            tee(capture, collected.sink, DISCARD),
            lambda: (_captured(capture), collected.observe()),
        )


@given(written=_streams(), data=st.data(), max_line_bytes=st.integers(1, 8))
def test_any_chunking_gives_each_streams_lines_decoded_and_cut_to_the_maximum(
    written: dict[OutputStream, bytes], data: st.DataObject, max_line_bytes: int
) -> None:
    collected = _CollectedLines(max_line_bytes=max_line_bytes)

    with collected.sink as sink:
        for stream, chunk in data.draw(_schedule(written)):
            sink.write(stream, chunk)

    assert collected.lines == {
        stream: _expected_lines(written[stream], max_line_bytes) for stream in OutputStream
    }


@given(written=_streams(), data=st.data(), limit=st.integers(0, 64))
def test_capture_keeps_at_most_its_limit_and_counts_exactly_what_it_dropped(
    written: dict[OutputStream, bytes], data: st.DataObject, limit: int
) -> None:
    capture = Capture(limit)

    for stream, chunk in data.draw(_schedule(written)):
        capture.write(stream, chunk)

    for stream, whole in written.items():
        assert capture.kept(stream) == whole[:limit]
        assert capture.omitted(stream) == max(len(whole) - limit, 0)
    assert (capture.stdout, capture.stderr) == (
        written[OutputStream.STDOUT][:limit],
        written[OutputStream.STDERR][:limit],
    )


@given(written=_streams(), data=st.data(), fan_out=st.integers(2, 4))
def test_tee_delivers_the_same_streams_to_every_sink(
    written: dict[OutputStream, bytes], data: st.DataObject, fan_out: int
) -> None:
    captures = [Capture(1024) for _ in range(fan_out)]
    sink = tee(*captures)

    for stream, chunk in data.draw(_schedule(written)):
        sink.write(stream, chunk)

    for capture in captures:
        assert (capture.stdout, capture.stderr) == (
            written[OutputStream.STDOUT],
            written[OutputStream.STDERR],
        )


def test_a_tee_of_nothing_but_discard_is_discard_and_of_one_sink_is_that_sink() -> None:
    capture = Capture(1)

    assert tee() is DISCARD
    assert tee(DISCARD, DISCARD) is DISCARD
    assert tee(DISCARD, capture) is capture


def test_lines_delivers_an_unterminated_last_line_when_its_run_raises() -> None:
    collected = _CollectedLines()

    def cut_short_build(sink: Lines) -> None:
        sink.write(OutputStream.STDERR, b"#1 [1/2] RUN make\n#2 [2/2] RUN te")
        message = "the build was cut short"
        raise TimeoutError(message)

    with pytest.raises(TimeoutError), collected.sink as sink:
        cut_short_build(sink)

    assert collected.lines[OutputStream.STDERR] == ["#1 [1/2] RUN make", "#2 [2/2] RUN te"]


def test_limits_below_their_minimum_are_refused() -> None:
    for build in (lambda: Capture(-1), lambda: Lines(lambda _s, _l: None, max_line_bytes=0)):
        try:
            build()
        except ValueError:
            continue
        message = "a limit below its minimum was accepted"
        raise AssertionError(message)
