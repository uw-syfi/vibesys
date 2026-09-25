"""Persistence and cursor tests for the run-event store."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, Unpack, cast

import pytest
from pydantic import ValidationError

from server.events import EventStore, EventType, RunEvent, ToolCallData, make_event

if TYPE_CHECKING:
    from types import TracebackType
    from typing import TextIO


class _PathOpenOptions(TypedDict, total=False):
    buffering: int
    encoding: str | None
    errors: str | None
    newline: str | None


def _persisted_event(sequence: int, text: str = "") -> RunEvent:
    return RunEvent(
        sequence=sequence,
        run_id="persisted-run",
        timestamp=datetime.now(UTC),
        type=EventType.OUTPUT,
        text=text,
    )


def _assign(target: object, field: str, value: object) -> None:
    """Assign a frozen model field, whose rejection is a runtime contract."""
    setattr(target, field, value)


def _write_events(path: Path, events: list[RunEvent]) -> None:
    path.write_text("".join(event.model_dump_json() + "\n" for event in events))


class TestEventStore:
    def test_startup_replay_cursor_and_next_sequence(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_persisted_event(1, "one"), _persisted_event(2, "two")])

        store = EventStore(path, run_id="active-run")

        assert [event.text for event in store.read()] == ["one", "two"]
        assert [event.text for event in store.read(after_sequence=1)] == ["two"]
        assert store.read(after_sequence=2) == []
        appended = store.append(make_event(EventType.OUTPUT, "three"))
        assert appended.sequence == 3
        assert appended.run_id == "active-run"
        assert [event.sequence for event in store.read(after_sequence=1)] == [2, 3]

    def test_legacy_out_of_order_sequences_get_stable_monotonic_cursors(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(
            path,
            [_persisted_event(2, "two"), _persisted_event(1, "one"), _persisted_event(3, "three")],
        )
        original = path.read_bytes()

        store = EventStore(path, run_id="active-run")

        assert [(event.sequence, event.text) for event in store.read()] == [
            (2, "two"),
            (3, "one"),
            (4, "three"),
        ]
        assert path.read_bytes() == original
        assert [event.text for event in store.read(after_sequence=2)] == ["one", "three"]
        assert store.append(make_event(EventType.OUTPUT, "four")).sequence == 5

        reopened = EventStore(path, run_id="reopened-run")
        assert [(event.sequence, event.text) for event in reopened.read()] == [
            (2, "two"),
            (3, "one"),
            (4, "three"),
            (5, "four"),
        ]

    def test_legacy_duplicate_sequences_preserve_every_payload(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(
            path,
            [
                _persisted_event(1, "one"),
                _persisted_event(2, "first two"),
                _persisted_event(2, "second two"),
                _persisted_event(3, "three"),
            ],
        )

        store = EventStore(path, run_id="active-run")

        assert [(event.sequence, event.text) for event in store.read()] == [
            (1, "one"),
            (2, "first two"),
            (3, "second two"),
            (4, "three"),
        ]
        assert [event.text for event in store.read(after_sequence=2)] == [
            "second two",
            "three",
        ]

    def test_legacy_sequence_resets_replay_every_payload_across_cursors(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "events.jsonl"
        raw_sequences = [*range(7690, 7711), *range(7690, 7715), *range(7711, 7738)]
        _write_events(
            path,
            [
                _persisted_event(sequence, f"payload-{index}")
                for index, sequence in enumerate(raw_sequences)
            ],
        )
        store = EventStore(path, run_id="active-run")

        replayed: list[RunEvent] = []
        cursor = 0
        while batch := store.read(after_sequence=cursor)[:7]:
            replayed.extend(batch)
            cursor = batch[-1].sequence

        assert [event.text for event in replayed] == [
            f"payload-{index}" for index in range(len(raw_sequences))
        ]
        assert all(previous.sequence < current.sequence for previous, current in pairwise(replayed))
        assert store.last_sequence == replayed[-1].sequence

    def test_repeated_tail_reads_do_not_reparse_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "events.jsonl"
        event_count = 1_000
        _write_events(path, [_persisted_event(index) for index in range(1, event_count + 1)])
        parse_count = 0
        parse = RunEvent.model_validate_json

        def counting_parse(raw: str) -> RunEvent:
            nonlocal parse_count
            parse_count += 1
            return parse(raw)

        monkeypatch.setattr(RunEvent, "model_validate_json", counting_parse)
        store = EventStore(path, run_id="active-run")
        assert parse_count == event_count

        for _ in range(20):
            assert store.read(event_count) == []
            assert store.wait(event_count, timeout=0) == []

        assert parse_count == event_count

    def test_ignores_only_a_malformed_final_record(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        valid = _persisted_event(1, "complete").model_dump_json()
        path.write_text(valid + "\n" + '{"protocol_version":1')

        store = EventStore(path, run_id="active-run")

        assert [event.text for event in store.read()] == ["complete"]

    @pytest.mark.parametrize("tail", ['{"protocol_version":1', '{"protocol_version":1\n'])
    def test_append_repairs_ignored_malformed_tail_before_writing(
        self, tmp_path: Path, tail: str
    ) -> None:
        path = tmp_path / "events.jsonl"
        valid = _persisted_event(1, "complete").model_dump_json()
        path.write_text(valid + "\n" + tail)
        store = EventStore(path, run_id="active-run")

        store.append(make_event(EventType.OUTPUT, "after repair"))
        reopened = EventStore(path, run_id="reopened-run")

        assert [(event.sequence, event.text) for event in reopened.read()] == [
            (1, "complete"),
            (2, "after repair"),
        ]

    def test_append_preserves_a_valid_unterminated_final_record(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        path.write_text(_persisted_event(1, "unterminated").model_dump_json())
        store = EventStore(path, run_id="active-run")
        assert [event.text for event in store.read()] == ["unterminated"]

        store.append(make_event(EventType.OUTPUT, "appended"))

        assert [event.text for event in store.read()] == ["unterminated", "appended"]
        reopened = EventStore(path, run_id="reopened-run")
        assert [(event.sequence, event.text) for event in reopened.read()] == [
            (1, "unterminated"),
            (2, "appended"),
        ]
        raw = path.read_text()
        assert raw.endswith("\n")
        assert [json.loads(line)["text"] for line in raw.splitlines()] == [
            "unterminated",
            "appended",
        ]

    def test_append_writes_no_repair_newline_after_a_terminated_tail(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_persisted_event(1, "one")])
        original = path.read_bytes()
        store = EventStore(path, run_id="active-run")

        appended = store.append(make_event(EventType.OUTPUT, "two"))

        assert path.read_bytes() == original + (appended.model_dump_json() + "\n").encode()

    def test_a_missing_final_newline_is_repaired_exactly_once(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        path.write_text(_persisted_event(1, "unterminated").model_dump_json())
        store = EventStore(path, run_id="active-run")

        store.append(make_event(EventType.OUTPUT, "second"))
        store.append(make_event(EventType.OUTPUT, "third"))

        raw = path.read_text()
        assert "\n\n" not in raw
        assert len(raw.splitlines()) == 3
        assert [event.text for event in EventStore(path, run_id="reopened-run").read()] == [
            "unterminated",
            "second",
            "third",
        ]

    def test_append_after_external_removal_starts_the_new_file_cleanly(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "events.jsonl"
        path.write_text(_persisted_event(1, "unterminated").model_dump_json())
        store = EventStore(path, run_id="active-run")
        path.unlink()

        appended = store.append(make_event(EventType.OUTPUT, "fresh"))

        assert path.read_bytes() == (appended.model_dump_json() + "\n").encode()
        assert [event.text for event in EventStore(path, run_id="reopened-run").read()] == ["fresh"]

    def test_concatenated_final_records_raise_instead_of_truncating(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        first = _persisted_event(1, "first").model_dump_json()
        second = _persisted_event(2, "second").model_dump_json()
        third = _persisted_event(3, "third").model_dump_json()
        path.write_text(first + "\n" + second + third + "\n")
        original = path.read_bytes()

        with pytest.raises(ValueError, match=f"byte offset {len(first) + 1} "):
            EventStore(path, run_id="active-run")

        assert path.read_bytes() == original

    def test_a_complete_but_invalid_final_record_raises_instead_of_truncating(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "events.jsonl"
        valid = _persisted_event(1, "complete").model_dump_json()
        record = json.loads(_persisted_event(2, "terminal").model_dump_json())
        record["data"] = {"kind": "output", "stream": "stdout", "content": 5}
        path.write_text(valid + "\n" + json.dumps(record) + "\n")
        original = path.read_bytes()

        with pytest.raises(ValueError, match=f"byte offset {len(valid) + 1} ") as excinfo:
            EventStore(path, run_id="active-run")

        assert str(path) in str(excinfo.value)
        assert path.read_bytes() == original

    def test_rejects_a_malformed_record_before_the_tail(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        first = _persisted_event(1).model_dump_json()
        last = _persisted_event(2).model_dump_json()
        path.write_text(first + "\nnot-json\n" + last + "\n")

        with pytest.raises(ValueError, match="Invalid JSON"):
            EventStore(path, run_id="active-run")

    def test_append_wakes_multiple_independent_readers(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "events.jsonl", run_id="active-run")
        ready = threading.Barrier(3)

        def wait_for_first_event() -> list[RunEvent]:
            ready.wait()
            return store.wait(after_sequence=0, timeout=2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            readers = [executor.submit(wait_for_first_event) for _ in range(2)]
            ready.wait()
            appended = store.append(make_event(EventType.OUTPUT, "visible"))

        batches = [reader.result() for reader in readers]
        assert [[event.sequence for event in batch] for batch in batches] == [[1], [1]]
        assert all(batch[0] == appended for batch in batches)
        # Readers share the stored event rather than each getting a copy: it is
        # frozen, so sharing is what keeps replay from copying whole histories.
        assert batches[0][0] is batches[1][0]
        assert batches[0] is not batches[1]

    def test_cached_events_are_isolated_from_reader_mutation(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "events.jsonl", run_id="active-run")
        appended = store.append(make_event(EventType.OUTPUT, "durable"))

        first_read = store.read()
        with pytest.raises(ValidationError):
            _assign(first_read[0], "text", "mutated")

        assert appended.text == "durable"
        assert store.read()[0].text == "durable"

    def test_reader_projections_do_not_disturb_stored_history(self, tmp_path: Path) -> None:
        """The read path builds variants by copy, which the store never sees."""
        store = EventStore(tmp_path / "events.jsonl", run_id="active-run")
        store.append(make_event(EventType.OUTPUT, "durable"))

        projected = store.read()[0].model_copy(update={"text": "projected"})

        assert projected.text == "projected"
        assert store.read()[0].text == "durable"

    def test_appended_events_are_immutable(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "events.jsonl", run_id="active-run")
        appended = store.append(
            make_event(EventType.TOOL_CALL, data=ToolCallData(tool="Bash", call_id="c1"))
        )

        with pytest.raises(ValidationError):
            _assign(appended, "sequence", 99)
        assert isinstance(appended.data, ToolCallData)
        with pytest.raises(ValidationError):
            _assign(appended.data, "tool", "Write")

    def test_append_does_not_publish_cache_state_when_file_close_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "events.jsonl"
        store = EventStore(path, run_id="active-run")
        real_open = Path.open

        class FailingCloseStream:
            def __enter__(self) -> FailingCloseStream:
                return self

            def write(self, _text: str) -> None:
                return None

            def __exit__(
                self,
                _exc_type: type[BaseException] | None,
                _exc_value: BaseException | None,
                _traceback: TracebackType | None,
            ) -> bool | None:
                _failure_message = "close failed"
                raise OSError(_failure_message)

        def open_with_close_failure(
            target: Path,
            mode: str = "r",
            **options: Unpack[_PathOpenOptions],
        ) -> TextIO | FailingCloseStream:
            if target == path and mode == "a":
                return FailingCloseStream()
            return cast(
                "TextIO",
                real_open(target, mode, **options),
            )

        monkeypatch.setattr(Path, "open", open_with_close_failure)

        with pytest.raises(OSError, match="close failed"):
            store.append(make_event(EventType.OUTPUT, "ghost"))

        assert store.read() == []
        assert store.last_sequence == 0
