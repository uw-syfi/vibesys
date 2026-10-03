"""Contract tests for the generic durable event journal."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from vs_runtime.api.infrastructure import DurableEventJournal, EventCodec

if TYPE_CHECKING:
    from pathlib import Path


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


def _journal() -> DurableEventJournal[_Event]:
    return DurableEventJournal(_Codec(), filename="events.jsonl")


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
    journal = _journal()
    journal.attach(tmp_path, "run-1")
    journal.record(_Event("started"))

    assert [event.kind for event in journal.wait(0)] == ["started"]
    assert journal.wait(1, timeout=0) == []


def test_concurrent_records_receive_one_contiguous_sequence(tmp_path: Path) -> None:
    journal = _journal()
    journal.attach(tmp_path, "run-1")

    with ThreadPoolExecutor(max_workers=8) as executor:
        recorded = list(executor.map(lambda index: journal.record(_Event(str(index))), range(64)))

    assert sorted(event.sequence for event in recorded) == list(range(1, 65))
    assert [event.sequence for event in journal.read()] == list(range(1, 65))
