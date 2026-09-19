"""Persistence and cursor tests for the run-event store."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path

import pytest

import server.events as events_module
from server.events import EventStore, _scan_header_fields, parse_event
from server.wire import codec, messages
from server.wire.v2 import events_pb2

ET = events_pb2.EventType
RunEvent = events_pb2.RunEvent


def make_event(text: str = "") -> RunEvent:
    return messages.make_event(ET.EVENT_TYPE_OUTPUT, text)


def _persisted_event(sequence: int, text: str = "") -> RunEvent:
    event = messages.make_event(ET.EVENT_TYPE_OUTPUT, text)
    event.sequence = sequence
    event.run_id = "persisted-run"
    return event


def _v1_line(sequence: int, text: str = "", **extra: object) -> str:
    """One journal line as the Pydantic-era recorder wrote it (protocol version 1)."""
    record = {
        "protocol_version": 1,
        "sequence": sequence,
        "run_id": "persisted-run",
        "timestamp": "2026-01-01T00:00:00Z",
        "type": "output",
        "text": text,
        **extra,
    }
    return json.dumps(record)


def _write_events(path: Path, events: list[RunEvent]) -> None:
    path.write_text("".join(codec.dumps(event) + "\n" for event in events))


class TestEventStore:
    def test_startup_replay_cursor_and_next_sequence(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "events.jsonl"
        _write_events(path, [_persisted_event(1, "one"), _persisted_event(2, "two")])

        store = EventStore(path, run_id="active-run")

        assert [event.text for event in store.read()] == ["one", "two"]
        assert [event.text for event in store.read(after_sequence=1)] == ["two"]
        assert store.read(after_sequence=2) == []
        appended = store.append(make_event("three"))
        assert appended.sequence == 3
        assert appended.run_id == "active-run"
        assert [event.sequence for event in store.read(after_sequence=1)] == [2, 3]

    def test_legacy_out_of_order_sequences_get_stable_monotonic_cursors(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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
        assert store.append(make_event("four")).sequence == 5

        reopened = EventStore(path, run_id="reopened-run")
        assert [(event.sequence, event.text) for event in reopened.read()] == [
            (2, "two"),
            (3, "one"),
            (4, "three"),
            (5, "four"),
        ]

    def test_legacy_duplicate_sequences_preserve_every_payload(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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

    def test_legacy_sequence_resets_replay_every_payload_across_cursors(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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

    def test_repeated_tail_reads_do_not_reparse_history(self, tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "events.jsonl"
        event_count = 1_000
        _write_events(path, [_persisted_event(index) for index in range(1, event_count + 1)])
        parse_count = 0
        parse = events_module.parse_event

        def counting_parse(raw):  # noqa: ANN001, ANN202  # tracked: #288
            nonlocal parse_count
            parse_count += 1
            return parse(raw)

        monkeypatch.setattr(events_module, "parse_event", counting_parse)
        store = EventStore(path, run_id="active-run")
        assert parse_count == event_count

        for _ in range(20):
            assert store.read(event_count) == []
            assert store.wait(event_count, timeout=0) == []

        assert parse_count == event_count

    def test_ignores_only_a_malformed_final_record(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "events.jsonl"
        valid = codec.dumps(_persisted_event(1, "complete"))
        path.write_text(valid + "\n" + '{"protocol_version":1')

        store = EventStore(path, run_id="active-run")

        assert [event.text for event in store.read()] == ["complete"]

    @pytest.mark.parametrize("tail", ['{"protocol_version":1', '{"protocol_version":1\n'])
    def test_append_repairs_ignored_malformed_tail_before_writing(self, tmp_path, tail):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "events.jsonl"
        valid = codec.dumps(_persisted_event(1, "complete"))
        path.write_text(valid + "\n" + tail)
        store = EventStore(path, run_id="active-run")

        store.append(make_event("after repair"))
        reopened = EventStore(path, run_id="reopened-run")

        assert [(event.sequence, event.text) for event in reopened.read()] == [
            (1, "complete"),
            (2, "after repair"),
        ]

    def test_append_preserves_a_valid_unterminated_final_record(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "events.jsonl"
        path.write_text(codec.dumps(_persisted_event(1, "unterminated")))
        store = EventStore(path, run_id="active-run")
        assert [event.text for event in store.read()] == ["unterminated"]

        store.append(make_event("appended"))

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

    def test_append_writes_no_repair_newline_after_a_terminated_tail(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "events.jsonl"
        _write_events(path, [_persisted_event(1, "one")])
        original = path.read_bytes()
        store = EventStore(path, run_id="active-run")

        appended = store.append(make_event("two"))

        assert path.read_bytes() == original + (codec.dumps(appended) + "\n").encode()

    def test_a_missing_final_newline_is_repaired_exactly_once(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "events.jsonl"
        path.write_text(codec.dumps(_persisted_event(1, "unterminated")))
        store = EventStore(path, run_id="active-run")

        store.append(make_event("second"))
        store.append(make_event("third"))

        raw = path.read_text()
        assert "\n\n" not in raw
        assert len(raw.splitlines()) == 3
        assert [event.text for event in EventStore(path, run_id="reopened-run").read()] == [
            "unterminated",
            "second",
            "third",
        ]

    def test_append_after_external_removal_starts_the_new_file_cleanly(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "events.jsonl"
        path.write_text(codec.dumps(_persisted_event(1, "unterminated")))
        store = EventStore(path, run_id="active-run")
        path.unlink()

        appended = store.append(make_event("fresh"))

        assert path.read_bytes() == (codec.dumps(appended) + "\n").encode()
        assert [event.text for event in EventStore(path, run_id="reopened-run").read()] == ["fresh"]

    def test_concatenated_final_records_raise_instead_of_truncating(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "events.jsonl"
        first = codec.dumps(_persisted_event(1, "first"))
        second = codec.dumps(_persisted_event(2, "second"))
        third = codec.dumps(_persisted_event(3, "third"))
        path.write_text(first + "\n" + second + third + "\n")
        original = path.read_bytes()

        with pytest.raises(ValueError, match=f"byte offset {len(first) + 1} "):
            EventStore(path, run_id="active-run")

        assert path.read_bytes() == original

    def test_a_complete_but_invalid_final_record_raises_instead_of_truncating(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "events.jsonl"
        valid = codec.dumps(_persisted_event(1, "complete"))
        record = json.loads(codec.dumps(_persisted_event(2, "terminal")))
        record["output"] = {"stream": "OUTPUT_STREAM_STDOUT", "content": 5}
        path.write_text(valid + "\n" + json.dumps(record) + "\n")
        original = path.read_bytes()

        with pytest.raises(ValueError, match=f"byte offset {len(valid) + 1} ") as excinfo:
            EventStore(path, run_id="active-run")

        assert str(path) in str(excinfo.value)
        assert path.read_bytes() == original

    def test_rejects_a_malformed_record_before_the_tail(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "events.jsonl"
        first = codec.dumps(_persisted_event(1))
        last = codec.dumps(_persisted_event(2))
        path.write_text(first + "\nnot-json\n" + last + "\n")

        with pytest.raises(ValueError):  # noqa: PT011  # tracked: #288
            EventStore(path, run_id="active-run")

    def test_append_wakes_multiple_independent_readers(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        store = EventStore(tmp_path / "events.jsonl", run_id="active-run")
        ready = threading.Barrier(3)

        def wait_for_first_event() -> list[RunEvent]:
            ready.wait()
            return store.wait(after_sequence=0, timeout=2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            readers = [executor.submit(wait_for_first_event) for _ in range(2)]
            ready.wait()
            appended = store.append(make_event("visible"))

        batches = [reader.result() for reader in readers]
        assert [[event.sequence for event in batch] for batch in batches] == [[1], [1]]
        assert all(batch[0] == appended for batch in batches)
        # Readers share the stored event rather than each getting a copy: it is
        # frozen, so sharing is what keeps replay from copying whole histories.
        assert batches[0][0] is batches[1][0]
        assert batches[0] is not batches[1]

    def test_reads_share_stored_events_and_projections_do_not_disturb_history(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """Generated messages are mutable, so the store shares them and readers derive copies.

        Replaces the Pydantic frozen-model checks: immutability is now a
        convention (``messages.replace``), not enforced by the type.
        """
        store = EventStore(tmp_path / "events.jsonl", run_id="active-run")
        appended = store.append(make_event("durable"))

        first_read = store.read()
        assert first_read[0] is appended
        assert store.read()[0] is first_read[0]

        projected = messages.replace(first_read[0], text="projected")

        assert projected.text == "projected"
        assert appended.text == "durable"
        assert store.read()[0].text == "durable"

    def test_append_does_not_alias_or_mutate_the_input_event(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        store = EventStore(tmp_path / "events.jsonl", run_id="active-run")
        event = messages.make_event(
            ET.EVENT_TYPE_TOOL_CALL, data=events_pb2.ToolCallData(tool="Bash", call_id="c1")
        )

        appended = store.append(event)

        assert appended is not event
        assert (event.sequence, event.run_id) == (0, "")
        assert (appended.sequence, appended.run_id) == (1, "active-run")
        assert appended.tool_call.tool == "Bash"

    def test_mixed_v1_and_v2_file_loads_with_the_same_events(self, tmp_path):  # noqa: ANN001, ANN201
        """A journal written across the wire upgrade replays as one sequence."""
        v1_path = tmp_path / "v1.jsonl"
        mixed_path = tmp_path / "mixed.jsonl"
        v1_path.write_text(
            _v1_line(1, "one", invocation_id="ex-1") + "\n" + _v1_line(2, "two") + "\n"
        )
        mixed_path.write_text(
            _v1_line(1, "one", invocation_id="ex-1")
            + "\n"
            + codec.dumps(parse_event(_v1_line(2, "two")))
            + "\n"
        )

        v1_events = EventStore(v1_path, run_id="r").read()
        mixed_events = EventStore(mixed_path, run_id="r").read()

        assert [(e.sequence, e.text, e.type) for e in mixed_events] == [
            (1, "one", ET.EVENT_TYPE_OUTPUT),
            (2, "two", ET.EVENT_TYPE_OUTPUT),
        ]
        assert mixed_events[0].execution_id == "ex-1"
        assert [e.protocol_version for e in mixed_events] == [2, 2]
        assert mixed_events == v1_events
        # Reading never rewrites the file.
        assert mixed_path.read_text().splitlines()[0] == _v1_line(1, "one", invocation_id="ex-1")

    def test_appending_to_a_v1_journal_writes_v2_lines(self, tmp_path):  # noqa: ANN001, ANN201
        path = tmp_path / "events.jsonl"
        path.write_text(_v1_line(1, "old") + "\n")
        store = EventStore(path, run_id="active-run")

        store.append(make_event("new"))

        lines = path.read_text().splitlines()
        assert json.loads(lines[0])["type"] == "output"
        assert json.loads(lines[1])["type"] == "EVENT_TYPE_OUTPUT"
        assert [e.text for e in EventStore(path, run_id="r").read()] == ["old", "new"]


class TestScanHeaderFields:
    def test_v1_record_folds_invocation_id_and_reads_lowercase_type(self):  # noqa: ANN201
        line = _v1_line(4, "x", invocation_id="inv", chat_thread_id="t").encode()

        assert _scan_header_fields(line) == (4, ET.EVENT_TYPE_OUTPUT, "inv", "t")

    def test_v2_record_reads_prefixed_type_and_execution_id(self):  # noqa: ANN201
        event = _persisted_event(4, "x")
        event.execution_id = "ex"
        event.chat_thread_id = "t"

        assert _scan_header_fields(codec.dumps(event).encode()) == (
            4,
            ET.EVENT_TYPE_OUTPUT,
            "ex",
            "t",
        )

    @pytest.mark.parametrize(
        "record",
        [
            {"protocol_version": 2, "sequence": 1, "type": "output"},
            {"protocol_version": 1, "sequence": 1, "type": "EVENT_TYPE_OUTPUT"},
            {"protocol_version": 3, "sequence": 1, "type": "EVENT_TYPE_OUTPUT"},
            {"protocol_version": 2, "sequence": 1, "type": "EVENT_TYPE_OUTPUT", "execution_id": 5},
        ],
    )
    def test_mismatched_spelling_or_shape_defers_to_strict_parse(self, record):  # noqa: ANN001, ANN201
        assert _scan_header_fields(json.dumps(record).encode()) is None

    def test_append_does_not_publish_cache_state_when_file_close_fails(self, tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
        path = tmp_path / "events.jsonl"
        store = EventStore(path, run_id="active-run")
        real_open = Path.open

        class FailingCloseStream:
            def __enter__(self):  # noqa: ANN204  # tracked: #288
                return self

            def write(self, _text):  # noqa: ANN001, ANN202  # tracked: #288
                return None

            def __exit__(self, *_args):  # noqa: ANN002, ANN204  # tracked: #288
                raise OSError("close failed")  # noqa: TRY003  # tracked: #288

        def open_with_close_failure(target, mode="r", *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202  # tracked: #288
            if target == path and mode == "a":
                return FailingCloseStream()
            return real_open(target, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_with_close_failure)

        with pytest.raises(OSError, match="close failed"):
            store.append(make_event("ghost"))

        assert store.read() == []
        assert store.last_sequence == 0
