"""Contract tests for the generic durable event journal."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, ClassVar, Self, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api.infrastructure import DurableEventJournal, EventCodec
from vs_sim.api.testing import HANG_GUARD_S, SimThreads

if TYPE_CHECKING:
    from types import TracebackType


@dataclass(frozen=True, slots=True)
class _Event:
    kind: str
    sequence: int = 0
    stream_id: str = ""


class _Codec(EventCodec[_Event]):
    def decode(self, data: bytes) -> _Event:
        value = json.loads(data)
        try:
            return _Event(**value)
        except TypeError as error:
            raise ValueError from error

    def encode(self, event: _Event) -> bytes:
        return json.dumps(
            {"kind": event.kind, "sequence": event.sequence, "stream_id": event.stream_id}
        ).encode()

    def sequence(self, event: _Event) -> int:
        return event.sequence

    def stamp(self, event: _Event, *, sequence: int, stream_id: str) -> _Event:
        return replace(event, sequence=sequence, stream_id=stream_id)


class _FailOnceCodec(_Codec):
    def __init__(self, fail_kind: str) -> None:
        self._fail_kind = fail_kind

    def encode(self, event: _Event) -> bytes:
        if event.kind == self._fail_kind:
            self._fail_kind = ""
            message = "injected journal write failure"
            raise OSError(message)
        return super().encode(event)


class _WrappedBinaryStream:
    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream

    def __enter__(self) -> Self:
        self._stream.__enter__()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return self._stream.__exit__(exception_type, exception, traceback)

    def write(self, data: bytes) -> int:
        return self._stream.write(data)


class _CloseFailureStream(_WrappedBinaryStream):
    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        suppressed = super().__exit__(exception_type, exception, traceback)
        if exception_type is None:
            message = "injected failure after closing a complete record"
            raise OSError(message)
        return suppressed


class _ShortWriteStream(_WrappedBinaryStream):
    def write(self, data: bytes) -> int:
        written = max(1, len(data) // 2)
        self._stream.write(data[:written])
        return written


class _MissingDelimiterStream(_WrappedBinaryStream):
    def write(self, data: bytes) -> int:
        written = len(data) - 1
        self._stream.write(data[:written])
        return written


class _FaultPath(Path):
    _close_failure: ClassVar[Path | None] = None
    _missing_delimiter: ClassVar[Path | None] = None
    _short_write: ClassVar[Path | None] = None
    _read_failure: ClassVar[Path | None] = None

    @classmethod
    def fail_next_append(cls, path: Path) -> None:
        cls._close_failure = Path(path)

    @classmethod
    def short_next_append(cls, path: Path) -> None:
        cls._short_write = Path(path)

    @classmethod
    def omit_next_delimiter(cls, path: Path) -> None:
        cls._missing_delimiter = Path(path)

    @classmethod
    def fail_next_read(cls, path: Path) -> None:
        cls._read_failure = Path(path)

    @classmethod
    def _consume_close_failure(cls, path: Path) -> bool:
        if cls._close_failure != path:
            return False
        cls._close_failure = None
        return True

    @classmethod
    def _consume_short_write(cls, path: Path) -> bool:
        if cls._short_write != path:
            return False
        cls._short_write = None
        return True

    @classmethod
    def _consume_missing_delimiter(cls, path: Path) -> bool:
        if cls._missing_delimiter != path:
            return False
        cls._missing_delimiter = None
        return True

    @classmethod
    def _consume_read_failure(cls, path: Path) -> bool:
        if cls._read_failure != path:
            return False
        cls._read_failure = None
        return True

    def open(  # ty: ignore[invalid-method-override]  # LW-196501 [invalid-method-override]; pathlib's stub narrows subclass opens to text despite its IO[Any] declaration, while this faithful Path fake must wrap only the binary append stream to inject a post-close failure without patching private journal code.
        self,
        mode: str,
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> IO[Any]:
        stream = super().open(mode, buffering, encoding, errors, newline)
        if mode == "ab" and self._consume_close_failure(Path(self)):
            binary_stream = cast("IO[bytes]", stream)
            return cast("IO[bytes]", _CloseFailureStream(binary_stream))
        if mode == "ab" and self._consume_short_write(Path(self)):
            binary_stream = cast("IO[bytes]", stream)
            return cast("IO[bytes]", _ShortWriteStream(binary_stream))
        if mode == "ab" and self._consume_missing_delimiter(Path(self)):
            binary_stream = cast("IO[bytes]", stream)
            return cast("IO[bytes]", _MissingDelimiterStream(binary_stream))
        return stream

    def read_bytes(self) -> bytes:
        if self._consume_read_failure(Path(self)):
            message = "injected journal reload failure"
            raise OSError(message)
        return super().read_bytes()


def _journal(
    codec: EventCodec[_Event] | None = None, threads: SimThreads | None = None
) -> DurableEventJournal[_Event]:
    return DurableEventJournal(
        codec or _Codec(), filename="events.jsonl", threads=threads or SimThreads()
    )


def test_journal_flushes_pending_events_and_continues_sequence(tmp_path: Path) -> None:
    journal = _journal()
    observed: list[_Event] = []
    journal.subscribe(observed.append)

    pending = journal.record(_Event("started"))
    assert pending.sequence == 0
    assert observed == [pending]

    journal.attach(tmp_path, "run-1")
    finished = journal.record(_Event("finished"))

    assert finished.sequence == 2
    assert [event.sequence for event in journal.read()] == [1, 2]
    assert [event.stream_id for event in journal.read()] == ["run-1", "run-1"]
    assert len(observed) == 2
    assert (tmp_path / "events.jsonl").read_text().count("\n") == 2


@pytest.mark.parametrize("fail_index", [0, 1, 2])
def test_journal_retries_interrupted_pending_flush_in_order(
    tmp_path: Path, fail_index: int
) -> None:
    kinds = ["pending-0", "pending-1", "pending-2"]
    directory = tmp_path / str(fail_index)
    journal = _journal(_FailOnceCodec(kinds[fail_index]))
    for kind in kinds:
        journal.record(_Event(kind))

    with pytest.raises(OSError, match="injected journal write failure"):
        journal.attach(directory, "run-1")

    queued_during_recovery = journal.record(_Event("after-failure"))
    assert queued_during_recovery.sequence == 0
    journal.attach(directory, "run-1")

    expected_kinds = [*kinds, "after-failure"]
    assert [event.kind for event in journal.read()] == expected_kinds
    assert [event.sequence for event in journal.read()] == [1, 2, 3, 4]

    reopened = _journal()
    reopened.attach(directory, "run-1")
    assert [event.kind for event in reopened.read()] == expected_kinds


def test_journal_retries_when_opening_the_event_file_fails(tmp_path: Path) -> None:
    directory = tmp_path / "open-failure"
    directory.mkdir()
    event_path = directory / "events.jsonl"
    event_path.symlink_to(directory / "missing" / "events.jsonl")
    journal = _journal()
    journal.record(_Event("before-failure"))

    with pytest.raises(FileNotFoundError):
        journal.attach(directory, "run-1")

    journal.record(_Event("after-failure"))
    event_path.unlink()
    journal.attach(directory, "run-1")

    assert [event.kind for event in journal.read()] == ["before-failure", "after-failure"]
    assert [event.sequence for event in journal.read()] == [1, 2]


def test_journal_reconciles_a_complete_record_after_close_fails(tmp_path: Path) -> None:
    directory = _FaultPath(tmp_path / "close-failure")
    event_path = directory / "events.jsonl"
    journal = _journal()
    journal.record(_Event("ambiguous"))
    journal.record(_Event("pending"))
    _FaultPath.fail_next_append(event_path)

    with pytest.raises(OSError, match="failure after closing a complete record"):
        journal.attach(directory, "run-1")

    assert event_path.read_text().count("\n") == 1
    journal.record(_Event("after-failure"))
    journal.attach(directory, "run-1")

    expected_kinds = ["ambiguous", "pending", "after-failure"]
    assert [event.kind for event in journal.read()] == expected_kinds
    assert [event.sequence for event in journal.read()] == [1, 2, 3]
    reopened = _journal()
    reopened.attach(directory, "run-1")
    assert [event.kind for event in reopened.read()] == expected_kinds


def test_journal_keeps_reconciliation_pending_until_reload_succeeds(tmp_path: Path) -> None:
    directory = _FaultPath(tmp_path / "reload-failure")
    event_path = directory / "events.jsonl"
    journal = _journal()
    journal.record(_Event("ambiguous"))
    _FaultPath.fail_next_append(event_path)

    with pytest.raises(OSError, match="failure after closing a complete record"):
        journal.attach(directory, "run-1")
    _FaultPath.fail_next_read(event_path)
    with pytest.raises(OSError, match="injected journal reload failure"):
        journal.attach(directory, "run-1")

    journal.attach(directory, "run-1")
    later = journal.record(_Event("later"))

    assert [(event.kind, event.sequence) for event in journal.read()] == [
        ("ambiguous", 1),
        ("later", 2),
    ]
    assert later.sequence == 2
    reopened = _journal()
    reopened.attach(directory, "run-1")
    assert [(event.kind, event.sequence) for event in reopened.read()] == [
        ("ambiguous", 1),
        ("later", 2),
    ]


def test_journal_repairs_a_short_pending_write_before_retry(tmp_path: Path) -> None:
    directory = _FaultPath(tmp_path / "short-write")
    event_path = directory / "events.jsonl"
    journal = _journal()
    journal.record(_Event("first"))
    journal.record(_Event("second"))
    _FaultPath.short_next_append(event_path)

    with pytest.raises(OSError, match="incomplete event journal write"):
        journal.attach(directory, "run-1")
    journal.attach(directory, "run-1")

    assert [(event.kind, event.sequence) for event in journal.read()] == [
        ("first", 1),
        ("second", 2),
    ]
    reopened = _journal()
    reopened.attach(directory, "run-1")
    assert [(event.kind, event.sequence) for event in reopened.read()] == [
        ("first", 1),
        ("second", 2),
    ]


def test_journal_reconciles_a_complete_record_missing_its_delimiter(tmp_path: Path) -> None:
    directory = _FaultPath(tmp_path / "missing-delimiter")
    event_path = directory / "events.jsonl"
    journal = _journal()
    journal.record(_Event("once"))
    _FaultPath.omit_next_delimiter(event_path)

    with pytest.raises(OSError, match="incomplete event journal write"):
        journal.attach(directory, "run-1")
    assert not event_path.read_bytes().endswith(b"\n")
    journal.attach(directory, "run-1")

    assert [(event.kind, event.sequence) for event in journal.read()] == [("once", 1)]
    reopened = _journal()
    reopened.attach(directory, "run-1")
    assert [(event.kind, event.sequence) for event in reopened.read()] == [("once", 1)]
    assert event_path.read_text().count("\n") == 1


def test_journal_rejects_retargeting_a_pending_attach_retry(tmp_path: Path) -> None:
    first_directory = tmp_path / "first"
    second_directory = tmp_path / "second"
    journal = _journal(_FailOnceCodec("pending"))
    journal.record(_Event("pending"))

    with pytest.raises(OSError, match="injected journal write failure"):
        journal.attach(first_directory, "run-1")
    with pytest.raises(RuntimeError, match="cannot change journal path"):
        journal.attach(second_directory, "run-1")

    journal.attach(first_directory, "run-1")
    assert [(event.kind, event.sequence) for event in journal.read()] == [("pending", 1)]
    assert not (second_directory / "events.jsonl").exists()


def test_journal_rejects_changing_stream_during_an_attach_retry(tmp_path: Path) -> None:
    journal = _journal(_FailOnceCodec("second"))
    journal.record(_Event("first"))
    journal.record(_Event("second"))

    with pytest.raises(OSError, match="injected journal write failure"):
        journal.attach(tmp_path, "run-1")
    event_path = tmp_path / "events.jsonl"
    durable_prefix = event_path.read_bytes()

    with pytest.raises(RuntimeError, match="cannot change journal stream"):
        journal.attach(tmp_path, "run-2")
    assert event_path.read_bytes() == durable_prefix

    journal.attach(tmp_path, "run-1")
    assert [(event.kind, event.sequence, event.stream_id) for event in journal.read()] == [
        ("first", 1, "run-1"),
        ("second", 2, "run-1"),
    ]


def test_journal_replays_durable_history_and_unsubscribes(tmp_path: Path) -> None:
    first = _journal()
    first.attach(tmp_path, "run-1")
    first.record(_Event("started"))

    resumed = _journal()
    resumed.attach(tmp_path, "run-1")
    replayed: list[_Event] = []
    unsubscribe = resumed.subscribe(replayed.append, replay=True)
    resumed.record(_Event("finished"))
    unsubscribe()
    unsubscribe()
    resumed.record(_Event("ignored"))

    assert [event.kind for event in replayed] == ["started", "finished"]
    assert resumed.latest_sequence == 3


def test_journal_repairs_malformed_final_record_before_appending(tmp_path: Path) -> None:
    first = _journal()
    first.attach(tmp_path, "run-1")
    first.record(_Event("started"))
    path = tmp_path / "events.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"sequence":')

    resumed = _journal()
    resumed.attach(tmp_path, "run-1")
    resumed.record(_Event("finished"))

    verified = _journal()
    verified.attach(tmp_path, "run-1")
    assert [event.kind for event in verified.read()] == ["started", "finished"]
    assert len(path.read_text().splitlines()) == 2


def test_journal_separates_valid_final_record_without_newline(tmp_path: Path) -> None:
    first = _journal()
    first.attach(tmp_path, "run-1")
    first.record(_Event("started"))
    path = tmp_path / "events.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))

    resumed = _journal()
    resumed.attach(tmp_path, "run-1")
    resumed.record(_Event("finished"))

    assert len(path.read_text().splitlines()) == 2
    assert [event.sequence for event in resumed.read()] == [1, 2]


def test_wait_reads_available_events_and_supports_nonblocking_poll(tmp_path: Path) -> None:
    threads = SimThreads()
    journal = _journal(threads=threads)
    journal.attach(tmp_path, "run-1")
    journal.record(_Event("started"))

    def poll() -> None:
        assert [event.kind for event in journal.wait(0)] == ["started"]
        assert journal.wait(1, timeout=0) == []

    threads.run(poll)


@given(schedule_seed=st.integers(min_value=0, max_value=2**32 - 1))
def test_concurrent_records_receive_one_contiguous_sequence(
    tmp_path_factory: pytest.TempPathFactory, schedule_seed: int
) -> None:
    threads = SimThreads(schedule_seed=schedule_seed)
    journal = _journal(threads=threads)
    journal.attach(tmp_path_factory.mktemp("journal"), "run-1")
    recorded: list[_Event] = []

    def writer(first: int) -> None:
        recorded.extend(journal.record(_Event(str(index))) for index in range(first, 64, 8))

    def record_from_eight_threads() -> None:
        workers = [
            threads.spawn(partial(writer, first), name=f"writer-{first}", daemon=False)
            for first in range(8)
        ]
        for worker in workers:
            worker.join(HANG_GUARD_S)

    threads.run(record_from_eight_threads)

    assert sorted(event.sequence for event in recorded) == list(range(1, 65))
    assert [event.sequence for event in journal.read()] == list(range(1, 65))
