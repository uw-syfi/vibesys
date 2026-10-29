"""The ``QueueStatusReader`` Fake and the contract suite every implementation passes.

A test module registers an implementation by subclassing ``QueueStatusReaderContract``
and implementing ``reader_under_test``. Every case runs for every implementation: none
is skipped, so an implementation that cannot honour one is not a ``QueueStatusReader``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vs_decode.api import decode_json
from vs_evaluation.queue_status import (
    QUEUE_READING,
    QueueStatus,
    QueueStatusUnavailable,
)
from vs_evaluation.queue_status_wire import MAX_REPLY_BYTES

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_evaluation.queue_status import QueueStatusReader

_CASES = 8
"""Seeds each case runs; an implementation varies what it reads by seed."""


@dataclass
class FakeQueueStatusReader:
    """Answers the readings it holds, in order, repeating the last; counts its reads.

    ``readings`` may hold ``QueueStatusUnavailable`` to stand for a backend that is down.
    """

    readings: list[QueueStatus | QueueStatusUnavailable]
    reads: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        """Refuse an empty script: a reader always has an answer."""
        if not self.readings:
            message = "a FakeQueueStatusReader needs at least one reading"
            raise ValueError(message)

    def read(self) -> QueueStatus | QueueStatusUnavailable:
        """The next scripted reading, or the last one once the script is spent."""
        index = min(self.reads, len(self.readings) - 1)
        self.reads += 1
        return self.readings[index]


@dataclass(frozen=True)
class QueueStatusReaderUnderTest:
    """A reader and the harness that runs a call to it.

    ``run`` executes a call in the context the reader needs (a simulated network's
    threads, for one); the default calls it directly. ``expected`` is the reading the
    backend's state stands for, when the harness knows it.
    """

    reader: QueueStatusReader
    run: Callable[[Callable[[], object]], object] = field(default=lambda call: call())
    expected: QueueStatus | QueueStatusUnavailable | None = None


class QueueStatusReaderContract:
    """Cases for ``QueueStatusReader``. Implement :meth:`reader_under_test`."""

    def reader_under_test(self, seed: int) -> QueueStatusReaderUnderTest:
        """A fresh reader over backend state chosen by ``seed``."""
        raise NotImplementedError

    def test_a_reading_is_typed_and_survives_the_wire(self) -> None:
        """Every reading is a status or a typed unavailability the wire decodes back to itself."""
        for seed in range(_CASES):
            subject = self.reader_under_test(seed)
            reading = subject.run(subject.reader.read)
            assert isinstance(reading, QueueStatus | QueueStatusUnavailable), repr(reading)
            decoded = decode_json(
                reading.model_dump_json(),
                QUEUE_READING,
                source="queue status reading",
                max_bytes=MAX_REPLY_BYTES,
            )
            assert decoded == reading, repr(decoded)

    def test_reading_changes_nothing(self) -> None:
        """Reading twice over unchanged backend state gives the same reading."""
        for seed in range(_CASES):
            subject = self.reader_under_test(seed)
            first = subject.run(subject.reader.read)
            second = subject.run(subject.reader.read)
            assert first == second

    def test_a_reading_reports_the_backend_state(self) -> None:
        """Where the harness knows the backend's state, the reading reports exactly it."""
        for seed in range(_CASES):
            subject = self.reader_under_test(seed)
            reading = subject.run(subject.reader.read)
            if subject.expected is not None:
                assert reading == subject.expected

    def test_the_reader_offers_only_reading(self) -> None:
        """A reader has no public method but ``read``: nothing reached through it can act."""
        subject = self.reader_under_test(0)
        public = {
            name
            for name in dir(subject.reader)
            if not name.startswith("_") and callable(getattr(subject.reader, name))
        }
        assert public == {"read"}, sorted(public)


__all__ = [
    "FakeQueueStatusReader",
    "QueueStatusReaderContract",
    "QueueStatusReaderUnderTest",
]
