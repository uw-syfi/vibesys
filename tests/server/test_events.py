"""Serialization and persistence tests for the run-event wire contract."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vibesys.server.events import (
    AgentOutputChunkData,
    AgentStatusData,
    EventStore,
    EventType,
    RunEvent,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
    make_event,
)


def _persisted_event(sequence: int, text: str = "") -> RunEvent:
    return RunEvent(
        sequence=sequence,
        run_id="persisted-run",
        timestamp=datetime.now(UTC),
        type=EventType.OUTPUT,
        text=text,
    )


def _write_events(path: Path, events: list[RunEvent]) -> None:
    path.write_text("".join(event.model_dump_json() + "\n" for event in events))


def _round_trip(event: RunEvent) -> RunEvent:
    return RunEvent.model_validate_json(event.model_dump_json())


class TestNewEventDataRoundTrip:
    def test_tool_call(self):
        status = AgentStatusData(
            progress="Round 1/2",
            agent_label="Implementer",
            elapsed_seconds=1.5,
            input_tokens=1000,
            context_window=200_000,
        )
        event = make_event(
            EventType.TOOL_CALL,
            data=ToolCallData(tool="shell", args={"cmd": "ls", "count": 3}, status=status),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, ToolCallData)
        assert restored.data.tool == "shell"
        assert restored.data.args == {"cmd": "ls", "count": 3}
        assert restored.data.status == status

    def test_tool_result(self):
        event = make_event(
            EventType.TOOL_RESULT,
            data=ToolResultData(tool="shell", content="out", is_error=True),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, ToolResultData)
        assert restored.data.is_error is True

    def test_todo_update(self):
        event = make_event(
            EventType.TODO_UPDATE,
            data=TodoUpdateData(todos=[TodoItemData(content="a", status="pending")]),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, TodoUpdateData)
        assert restored.data.todos == [TodoItemData(content="a", status="pending")]

    def test_usage_update(self):
        event = make_event(
            EventType.USAGE_UPDATE,
            data=UsageUpdateData(input_tokens=5_000, context_window=1_000_000, model="m"),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, UsageUpdateData)
        assert restored.data.input_tokens == 5_000

    def test_agent_output_chunk_status_is_optional_and_round_trips(self):
        bare = make_event(
            EventType.AGENT_OUTPUT_CHUNK,
            data=AgentOutputChunkData(channel="assistant", content="hi"),
        )
        restored = _round_trip(bare)
        assert isinstance(restored.data, AgentOutputChunkData)
        assert restored.data.status is None

        status = AgentStatusData(agent_label="Judge", elapsed_seconds=0.5, input_tokens=10)
        rich = make_event(
            EventType.AGENT_OUTPUT_CHUNK,
            data=AgentOutputChunkData(channel="assistant", content="hi", status=status),
        )
        restored = _round_trip(rich)
        assert isinstance(restored.data, AgentOutputChunkData)
        assert restored.data.status == status


class TestBackwardCompatibility:
    def test_chunk_without_status_field_still_parses(self):
        """Events recorded by older backends omit the new optional fields."""
        raw = (
            '{"protocol_version": 1, "sequence": 3, "run_id": "r", '
            '"timestamp": "2026-01-01T00:00:00Z", "type": "agent_output_chunk", '
            '"data": {"kind": "agent_output_chunk", "channel": "tool", "content": "x"}}'
        )
        event = RunEvent.model_validate_json(raw)
        assert isinstance(event.data, AgentOutputChunkData)
        assert event.data.status is None

    def test_unknown_data_kind_rejected(self):
        raw = (
            '{"protocol_version": 1, "timestamp": "2026-01-01T00:00:00Z", '
            '"type": "output", "data": {"kind": "not_a_kind"}}'
        )
        with pytest.raises(ValueError):
            RunEvent.model_validate_json(raw)


class TestEventStore:
    def test_startup_replay_cursor_and_next_sequence(self, tmp_path):
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

    def test_legacy_out_of_order_sequences_keep_file_order_filtering(self, tmp_path):
        path = tmp_path / "events.jsonl"
        _write_events(
            path,
            [_persisted_event(2, "two"), _persisted_event(1, "one"), _persisted_event(3, "three")],
        )

        store = EventStore(path, run_id="active-run")

        assert [event.text for event in store.read(after_sequence=1)] == ["two", "three"]
        assert store.append(make_event(EventType.OUTPUT, "four")).sequence == 4
        assert [event.text for event in store.read(after_sequence=2)] == ["three", "four"]

    def test_repeated_tail_reads_do_not_reparse_history(self, tmp_path, monkeypatch):
        path = tmp_path / "events.jsonl"
        event_count = 1_000
        _write_events(path, [_persisted_event(index) for index in range(1, event_count + 1)])
        parse_count = 0
        parse = RunEvent.model_validate_json

        def counting_parse(raw):
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

    def test_ignores_only_a_malformed_final_record(self, tmp_path):
        path = tmp_path / "events.jsonl"
        valid = _persisted_event(1, "complete").model_dump_json()
        path.write_text(valid + "\n" + '{"protocol_version":1')

        store = EventStore(path, run_id="active-run")

        assert [event.text for event in store.read()] == ["complete"]

    def test_rejects_a_malformed_record_before_the_tail(self, tmp_path):
        path = tmp_path / "events.jsonl"
        first = _persisted_event(1).model_dump_json()
        last = _persisted_event(2).model_dump_json()
        path.write_text(first + "\nnot-json\n" + last + "\n")

        with pytest.raises(ValueError):
            EventStore(path, run_id="active-run")

    def test_append_wakes_multiple_independent_readers(self, tmp_path):
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
        assert batches[0][0] is not batches[1][0]

    def test_cached_events_are_isolated_from_reader_mutation(self, tmp_path):
        store = EventStore(tmp_path / "events.jsonl", run_id="active-run")
        appended = store.append(make_event(EventType.OUTPUT, "durable"))

        first_read = store.read()
        first_read[0].text = "mutated"

        assert appended.text == "durable"
        assert store.read()[0].text == "durable"
