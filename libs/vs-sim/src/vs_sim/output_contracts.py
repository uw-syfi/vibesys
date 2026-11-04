"""Contract suites for the output write side (:class:`OutputSink`) and read side (:class:`ByteSource`).

Each suite is a base class: a test module subclasses it as ``Test<Variant>`` and
implements the one factory method, so every implementation, real or Fake, runs the same
cases. Cases draw from seeded randomness, so each run makes the same draws.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_sim.byte_source import ByteSourceChangedError, SourceChange
from vs_sim.output import OutputStream
from vs_sim.randomness import SeededRandom

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.byte_source import ByteSource
    from vs_sim.output import OutputSink

_CASES = 25
"""Seeded cases per property."""
_BYTES = (b"a", b"b", b"\n", b"\r", b"\xc3", b"\xa9", b"\xff", b"\x00")
"""Bytes drawn for output: line endings and bytes that are not valid UTF-8 on their own."""


def _output(rng: SeededRandom, longest: int = 40) -> bytes:
    return b"".join(rng.choice(_BYTES) for _ in range(rng.randint(0, longest)))


def _chunks(rng: SeededRandom, data: bytes) -> list[bytes]:
    """``data`` cut at random points, empty chunks included."""
    cuts = sorted(rng.randint(0, len(data)) for _ in range(rng.randint(0, 6)))
    bounds = [0, *cuts, len(data)]
    return [data[start:end] for start, end in itertools.pairwise(bounds)]


def _schedule(
    rng: SeededRandom, written: dict[OutputStream, bytes]
) -> list[tuple[OutputStream, bytes]]:
    """Each stream's bytes in random chunks, the two streams randomly interleaved."""
    pending = {stream: _chunks(rng, data) for stream, data in written.items()}
    schedule: list[tuple[OutputStream, bytes]] = []
    while any(pending.values()):
        stream = rng.choice([stream for stream, chunks in pending.items() if chunks])
        schedule.append((stream, pending[stream].pop(0)))
    return schedule


@dataclass(frozen=True)
class OutputSinkUnderTest:
    """A fresh sink and a way to see what it made of everything written to it."""

    sink: OutputSink
    observe: Callable[[], object]
    """What the sink holds or delivered once the producer is done, as a comparable value."""


class OutputSinkContract:
    """Cases for :class:`~vs_sim.output.OutputSink`. Implement :meth:`sink_under_test`."""

    def sink_under_test(self) -> OutputSinkUnderTest:
        """A fresh sink with its observer."""
        raise NotImplementedError

    def test_any_chunking_and_interleaving_of_the_same_output_is_seen_the_same(self) -> None:
        """Chunk boundaries and the order of the two streams' writes carry no meaning."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            written = {stream: _output(rng) for stream in OutputStream}
            seen = []
            for _ in range(2):
                subject = self.sink_under_test()
                for stream, chunk in _schedule(rng, written):
                    subject.sink.write(stream, chunk)
                seen.append(subject.observe())
            assert seen[0] == seen[1], seed

    def test_empty_writes_are_nothing(self) -> None:
        """A sink given only empty chunks is seen as one given nothing."""
        untouched = self.sink_under_test()
        emptied = self.sink_under_test()
        for stream in (*OutputStream, *OutputStream):
            emptied.sink.write(stream, b"")
        assert emptied.observe() == untouched.observe()

    def test_any_bytes_on_either_stream_are_accepted(self) -> None:
        """Every byte value, valid UTF-8 or not, is taken on both streams without raising."""
        subject = self.sink_under_test()
        every_byte = bytes(range(256))
        for stream in OutputStream:
            subject.sink.write(stream, every_byte)
            subject.sink.write(stream, every_byte[::-1])
        subject.observe()


@dataclass(frozen=True)
class ByteSourceUnderTest:
    """A source and the writer-side operations that change what it holds."""

    source: ByteSource
    append: Callable[[bytes], None]
    """Append bytes, as a writer to its log."""
    truncate: Callable[[int], None]
    """Cut the bytes to the given length in place."""
    replace: Callable[[bytes], None]
    """Put other bytes in the source's place (a new file at the same path)."""


class ByteSourceContract:
    """Cases for :class:`~vs_sim.byte_source.ByteSource`. Implement :meth:`source_under_test`."""

    def source_under_test(self, initial: bytes) -> ByteSourceUnderTest:
        """A fresh source holding ``initial``."""
        raise NotImplementedError

    def test_reads_at_any_offsets_reassemble_the_bytes_as_they_grow(self) -> None:
        """Each read returns the bytes at its offset, at least one and at most the maximum."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            expected = _output(rng)
            subject = self.source_under_test(expected)
            for _ in range(rng.randint(1, 8)):
                grown = _output(rng, 12)
                subject.append(grown)
                expected += grown
                offset = rng.randint(0, len(expected) + 2)
                max_bytes = rng.randint(1, 16)
                read = subject.source.read(offset, max_bytes)
                assert read == expected[offset : offset + max_bytes], seed
                assert subject.source.size() == len(expected), seed

    def test_reading_from_the_start_in_steps_returns_every_byte_once(self) -> None:
        """A reader that advances by what it got reassembles the whole source."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            expected = _output(rng, 80)
            subject = self.source_under_test(expected)
            offset, read = 0, b""
            while chunk := subject.source.read(offset, rng.randint(1, 9)):
                read += chunk
                offset += len(chunk)
            assert read == expected, seed

    def test_a_read_at_or_past_the_end_is_empty(self) -> None:
        """No bytes lie at or past the end."""
        subject = self.source_under_test(b"abc")
        assert subject.source.read(3, 10) == b""
        assert subject.source.read(1000, 1) == b""

    def test_a_shrink_after_a_read_is_reported_from_then_on(self) -> None:
        """A source cut below what was seen raises, even after it grows back."""
        for observe in (lambda s: s.size(), lambda s: s.read(0, 4)):
            subject = self.source_under_test(b"0123456789")
            observe(subject.source)
            subject.truncate(4)
            for _ in range(2):
                for again in (lambda s: s.size(), lambda s: s.read(0, 4)):
                    _expect_change(again, subject.source, SourceChange.SHRANK)
                subject.append(b"0123456789")

    def test_a_replacement_after_a_read_is_reported_even_when_it_is_longer(self) -> None:
        """Other bytes in the source's place raise rather than read on as if the same."""
        subject = self.source_under_test(b"first")
        assert subject.source.read(0, 5) == b"first"
        subject.replace(b"second and longer")
        _expect_change(lambda s: s.read(5, 5), subject.source, SourceChange.REPLACED)
        _expect_change(lambda s: s.size(), subject.source, SourceChange.REPLACED)

    def test_a_change_before_anything_was_seen_is_not_reported(self) -> None:
        """A reader that saw nothing yet reads whatever is there now."""
        subject = self.source_under_test(b"0123456789")
        subject.truncate(2)
        assert subject.source.read(0, 10) == b"01"

    def test_a_negative_offset_or_an_empty_read_is_refused(self) -> None:
        """A read must start at an offset and ask for at least one byte."""
        subject = self.source_under_test(b"abc")
        for offset, max_bytes in ((-1, 1), (0, 0), (0, -3)):
            try:
                subject.source.read(offset, max_bytes)
            except ValueError:
                continue
            message = f"read({offset}, {max_bytes}) was accepted"
            raise AssertionError(message)


def _expect_change(
    observe: Callable[[ByteSource], object], source: ByteSource, change: SourceChange
) -> None:
    try:
        observe(source)
    except ByteSourceChangedError as error:
        reported = error.change
    else:
        reported = None
    assert reported is change, f"the source reported {reported}, not that it {change}"
