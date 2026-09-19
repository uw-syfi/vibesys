"""Equivalence and accounting tests for the lazily attached event store.

The store indexes a run log with a cheap scan and validates records only when
a read reaches them. That is an optimization, so the contract under test is
equivalence: for any log, the lazy store must hand out exactly the events the
fully eager path would, at every cursor and every bounded range.
"""

import json
import random
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.server.support import build_server_parts

from server.events import (
    _EAGER_TAIL_RECORDS,
    EventStore,
    _records_from_events,
    _repair_legacy_sequences,
)
from server.journal import _canonical_execution_events
from server.wire import codec, messages
from server.wire.v2 import events_pb2, snapshot_pb2

ET = events_pb2.EventType
RunEvent = events_pb2.RunEvent

_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)
_ENUM_NAME = re.compile(r"^[A-Z0-9]+(_[A-Z0-9]+)+$")
_ENUM_PREFIXES = (
    "EVENT_TYPE_",
    "OUTPUT_STREAM_",
    "ROUND_JUDGE_VERDICT_",
    "EXECUTION_ACTIVITY_MODE_",
)


class _EagerEventStore(EventStore):
    """The reference implementation: never trust the scan, always validate.

    This is the real fallback the store takes on any doubt, so the equivalence
    tests compare the two production paths against each other.
    """

    def _scan_unlocked(self):  # noqa: ANN202
        events, malformed_tail_offset = self._read_unlocked()
        return _records_from_events(_repair_legacy_sequences(events)), malformed_tail_offset


def _v1_lower(value: Any) -> Any:  # noqa: ANN401
    """Rewrite v2 enum names to the lower-case strings version 1 used."""
    if isinstance(value, dict):
        return {key: _v1_lower(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_v1_lower(item) for item in value]
    if isinstance(value, str) and _ENUM_NAME.match(value):
        for prefix in _ENUM_PREFIXES:
            if value.startswith(prefix):
                return value.removeprefix(prefix).lower()
    return value


def _v1_line(event: RunEvent) -> str:
    """Serialize an event the way the Pydantic-era recorder did (protocol version 1)."""
    record = codec.to_dict(event)
    kind = event.WhichOneof("data")
    body = record.pop(kind) if kind is not None else None
    record = _v1_lower(record)
    record["protocol_version"] = 1
    if kind is not None:
        record["data"] = {"kind": kind, **_v1_lower(body)}
    return json.dumps(record)


def _write_events(
    path: Path, events: list[RunEvent], *, as_v1: Callable[[int], bool] = lambda _index: False
) -> None:
    """Write a log; ``as_v1`` picks which records use the version 1 spelling."""
    path.write_text(
        "".join(
            (_v1_line(event) if as_v1(index) else codec.dumps(event)) + "\n"
            for index, event in enumerate(events)
        )
    )


def _dump(events: list[RunEvent]) -> list[str]:
    return [codec.dumps(event) for event in events]


def _legacy_sequence_plan(count: int) -> list[int]:
    """Raw sequence numbers in the shapes real legacy logs contain.

    Out of order, duplicated, and a mid-log reset that replays earlier numbers.
    """
    plan = list(range(1, count + 1))
    if count < 8:  # the shapes below need room
        return plan
    plan[1], plan[2] = plan[2], plan[1]
    plan[4] = plan[3]
    reset_start = count // 2
    reset_length = max(2, count // 4)
    plan[reset_start : reset_start + reset_length] = list(range(1, reset_length + 1))
    return plan


def _event(
    sequence: int,
    event_type: events_pb2.EventType,
    text: str = "",
    **fields: Any,  # noqa: ANN401
) -> RunEvent:
    event = messages.make_event(event_type, text, **fields)
    event.sequence = sequence
    event.run_id = "persisted-run"
    event.timestamp.CopyFrom(messages.from_datetime(_TIMESTAMP))
    return event


def _generated_events(
    seed: int, count: int, *, legacy_sequences: bool = False, legacy_invocations: bool = False
) -> list[RunEvent]:
    """Build a deterministic log mixing the event shapes a real run writes."""
    rng = random.Random(seed)  # noqa: S311  # deterministic fixture data, not crypto
    plan = _legacy_sequence_plan(count) if legacy_sequences else list(range(1, count + 1))
    events: list[RunEvent] = []
    open_execution: str | None = None
    for index, sequence in enumerate(plan):
        roll = rng.random()
        if roll < 0.06:
            open_execution = f"exec-{index}"
            data: object = (
                events_pb2.InvocationStartedData(system_prompt="sys", user_prompt=f"prompt-{index}")
                if legacy_invocations
                else events_pb2.AgentExecutionStartedData(
                    stage="implementer",
                    user_prompt=f"prompt-{index}",
                    activity=snapshot_pb2.AgentExecutionActivityData(
                        mode=snapshot_pb2.ExecutionActivityMode.EXECUTION_ACTIVITY_MODE_THINKING,
                        summary="Thinking",
                    ),
                )
            )
            events.append(
                _event(
                    sequence,
                    ET.EVENT_TYPE_INVOCATION_STARTED
                    if legacy_invocations
                    else ET.EVENT_TYPE_AGENT_EXECUTION_STARTED,
                    agent_kind="implementer",
                    round_label=f"round-{index}",
                    execution_id=open_execution,
                    data=data,
                )
            )
        elif roll < 0.12 and open_execution is not None:
            finished: object = (
                events_pb2.InvocationFinishedData()
                if legacy_invocations
                else events_pb2.AgentExecutionFinishedData()
            )
            finished.result.struct_value.update({"ok": True})  # type: ignore[attr-defined]
            events.append(
                _event(
                    sequence,
                    ET.EVENT_TYPE_INVOCATION_FINISHED
                    if legacy_invocations
                    else ET.EVENT_TYPE_AGENT_EXECUTION_FINISHED,
                    agent_kind="implementer",
                    round_label=f"round-{index}",
                    execution_id=open_execution,
                    data=finished,
                )
            )
            open_execution = None
        elif roll < 0.15:
            events.append(
                _event(
                    sequence,
                    ET.EVENT_TYPE_ROUND_FINISHED,
                    round_label=f"round-{index}",
                    data=events_pb2.RoundFinishedData(
                        attempts=1,
                        judge_verdict=events_pb2.RoundJudgeVerdict.ROUND_JUDGE_VERDICT_PASS,
                    ),
                )
            )
        else:
            content = "x" * rng.randint(1, 400)
            events.append(
                _event(
                    sequence,
                    ET.EVENT_TYPE_OUTPUT,
                    f"line-{index}",
                    data=events_pb2.OutputData(
                        stream=events_pb2.OutputStream.OUTPUT_STREAM_STDOUT, content=content
                    ),
                )
            )
    return events


def _assert_matches_eager_store(path: Path) -> None:
    """Assert the lazy store is byte-identical to the eager one, everywhere."""
    eager = _EagerEventStore(path, run_id="reference")
    lazy = EventStore(path, run_id="reference")

    assert lazy.last_sequence == eager.last_sequence
    assert _dump(lazy.read()) == _dump(eager.read())

    reference = eager.read()
    cursors = [0, 1, len(reference) // 3, len(reference) // 2, len(reference) - 1, 10**9]
    for cursor in cursors:
        assert _dump(lazy.read(cursor)) == _dump(eager.read(cursor)), f"cursor {cursor}"
        # A second read must come from the cache, not a reparse.
        assert _dump(lazy.read(cursor)) == _dump(eager.read(cursor)), f"cursor {cursor} repeated"

    bounds = [(0, 1), (0, len(reference) // 2), (len(reference) // 4, len(reference) // 2), (3, 4)]
    for after, before in bounds:
        assert _dump(lazy.read(after, before)) == _dump(eager.read(after, before)), (
            f"range ({after}, {before})"
        )

    # The legacy lifecycle translation must agree through both paths too.
    assert _dump(_canonical_execution_events(lazy.read())) == _dump(
        _canonical_execution_events(eager.read())
    )


@pytest.mark.parametrize("count", [1, 12, _EAGER_TAIL_RECORDS - 1, 4 * _EAGER_TAIL_RECORDS + 37])
def test_lazy_store_matches_eager_store_for_plain_logs(tmp_path, count):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    _write_events(path, _generated_events(seed=7, count=count))

    _assert_matches_eager_store(path)


@pytest.mark.parametrize("count", [12, 3 * _EAGER_TAIL_RECORDS + 5])
def test_v1_and_mixed_logs_read_the_same_events_as_a_v2_log(tmp_path, count):  # noqa: ANN001, ANN201
    """Version 1 records are upgraded on read, alone or interleaved with version 2."""
    events = _generated_events(seed=9, count=count, legacy_invocations=True)
    v2_path = tmp_path / "v2.jsonl"
    v1_path = tmp_path / "v1.jsonl"
    mixed_path = tmp_path / "mixed.jsonl"
    _write_events(v2_path, events)
    _write_events(v1_path, events, as_v1=lambda _index: True)
    _write_events(mixed_path, events, as_v1=lambda index: index % 3 != 0)
    assert json.loads(v1_path.read_text().splitlines()[0])["protocol_version"] == 1
    expected = _dump(EventStore(v2_path, run_id="reference").read())

    for path in (v1_path, mixed_path):
        assert _dump(EventStore(path, run_id="reference").read()) == expected
        _assert_matches_eager_store(path)


@pytest.mark.parametrize("count", [12, _EAGER_TAIL_RECORDS - 1, 3 * _EAGER_TAIL_RECORDS + 5])
def test_lazy_store_matches_eager_store_for_legacy_sequences(tmp_path, count):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    _write_events(path, _generated_events(seed=11, count=count, legacy_sequences=True))

    _assert_matches_eager_store(path)


@pytest.mark.parametrize("count", [12, 3 * _EAGER_TAIL_RECORDS + 5])
def test_lazy_store_matches_eager_store_for_legacy_invocations(tmp_path, count):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        _generated_events(seed=13, count=count, legacy_sequences=True, legacy_invocations=True),
    )

    _assert_matches_eager_store(path)


def test_lazy_store_preserves_the_original_log_bytes(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    _write_events(
        path, _generated_events(seed=3, count=3 * _EAGER_TAIL_RECORDS, legacy_sequences=True)
    )
    original = path.read_bytes()

    store = EventStore(path, run_id="active-run")
    store.read()

    assert path.read_bytes() == original


def test_a_corrupt_record_before_the_tail_still_raises_from_construction(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    events = _generated_events(seed=5, count=3 * _EAGER_TAIL_RECORDS)
    lines = [codec.dumps(event) for event in events]
    lines[17] = "not-json"
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(codec.WireError):
        EventStore(path, run_id="active-run")
    with pytest.raises(codec.WireError):
        _EagerEventStore(path, run_id="reference")


def test_a_corrupt_final_record_is_ignored_and_repaired_by_append(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    events = _generated_events(seed=5, count=3 * _EAGER_TAIL_RECORDS)
    path.write_text(
        "".join(codec.dumps(event) + "\n" for event in events) + '{"protocol_version":1'
    )

    store = EventStore(path, run_id="active-run")
    assert _dump(store.read()) == _dump(_EagerEventStore(path, run_id="reference").read())

    appended = store.append(messages.make_event(ET.EVENT_TYPE_OUTPUT, "after repair"))
    reopened = EventStore(path, run_id="reopened-run")

    assert [event.text for event in reopened.read(appended.sequence - 1)] == ["after repair"]
    assert len(reopened.read()) == len(events) + 1


def test_lazy_store_matches_eager_store_for_a_valid_unterminated_tail(tmp_path):  # noqa: ANN001, ANN201
    serialized = "".join(
        codec.dumps(event) + "\n" for event in _generated_events(seed=43, count=12)
    )
    path = tmp_path / "events.jsonl"
    path.write_text(serialized.rstrip("\n"))

    _assert_matches_eager_store(path)

    lazy_path = tmp_path / "lazy.jsonl"
    eager_path = tmp_path / "eager.jsonl"
    lazy_path.write_text(serialized.rstrip("\n"))
    eager_path.write_text(serialized.rstrip("\n"))
    appended = messages.make_event(ET.EVENT_TYPE_OUTPUT, "after repair")
    EventStore(lazy_path, run_id="reference").append(appended)
    _EagerEventStore(eager_path, run_id="reference").append(appended)

    assert lazy_path.read_bytes() == eager_path.read_bytes()
    # The repair terminates the final record instead of concatenating onto it.
    assert lazy_path.read_bytes().startswith(serialized.encode())


@pytest.mark.parametrize(
    "invalid",
    [
        # Version 1 spelling (also covers the upgrade path).
        '{"protocol_version":1,"sequence":99,"run_id":"persisted-run",'
        '"timestamp":"2026-01-01T00:00:00+00:00","type":"output",'
        '"data":{"kind":"output","stream":"stdout","content":5}}',
        '{"protocol_version":2,"sequence":99,"run_id":"persisted-run",'
        '"timestamp":"2026-01-01T00:00:00Z","type":"EVENT_TYPE_OUTPUT",'
        '"output":{"stream":"OUTPUT_STREAM_STDOUT","content":5}}',
    ],
)
def test_lazy_store_matches_eager_store_for_a_complete_invalid_tail(tmp_path, invalid):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    path.write_text(
        "".join(codec.dumps(event) + "\n" for event in _generated_events(seed=47, count=12))
        + invalid
        + "\n"
    )
    original = path.read_bytes()

    with pytest.raises(ValueError, match="complete final record") as lazy_error:
        EventStore(path, run_id="active-run")
    with pytest.raises(ValueError, match="complete final record") as eager_error:
        _EagerEventStore(path, run_id="reference")

    assert str(lazy_error.value) == str(eager_error.value)
    assert path.read_bytes() == original


def test_lazy_store_matches_eager_store_for_a_concatenated_tail(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    serialized = [codec.dumps(event) for event in _generated_events(seed=53, count=12)]
    path.write_text("\n".join(serialized[:-2]) + "\n" + serialized[-2] + serialized[-1] + "\n")
    original = path.read_bytes()

    with pytest.raises(ValueError, match="complete final record") as lazy_error:
        EventStore(path, run_id="active-run")
    with pytest.raises(ValueError, match="complete final record") as eager_error:
        _EagerEventStore(path, run_id="reference")

    assert str(lazy_error.value) == str(eager_error.value)
    assert path.read_bytes() == original


def test_a_bounded_read_only_parses_the_records_it_returns(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    count = 6 * _EAGER_TAIL_RECORDS
    _write_events(path, _generated_events(seed=17, count=count))

    store = EventStore(path, run_id="active-run")
    after_construction = store.parsed_record_count
    assert after_construction == _EAGER_TAIL_RECORDS

    window = store.read(100, 150)

    assert [event.sequence for event in window] == list(range(101, 150))
    assert store.parsed_record_count == after_construction + len(window)


def test_repeated_bounded_reads_reuse_the_cached_parse(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    _write_events(path, _generated_events(seed=19, count=4 * _EAGER_TAIL_RECORDS))
    store = EventStore(path, run_id="active-run")

    first = store.read(200, 400)
    parsed = store.parsed_record_count
    second = store.read(200, 400)

    assert store.parsed_record_count == parsed
    # Stored events are shared, not copied, so the same objects come back.
    assert all(left is right for left, right in zip(first, second, strict=True))


def test_read_sequences_parses_only_the_named_records(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    _write_events(path, _generated_events(seed=23, count=4 * _EAGER_TAIL_RECORDS))
    store = EventStore(path, run_id="active-run")
    parsed = store.parsed_record_count

    events = store.read_sequences([5, 900, 2_000, 10**9])

    assert [event.sequence for event in events] == [5, 900, 2_000]
    assert store.parsed_record_count == parsed + 3


def test_headers_describe_every_record_without_parsing_it(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    events = _generated_events(seed=29, count=3 * _EAGER_TAIL_RECORDS, legacy_sequences=True)
    _write_events(path, events)
    store = EventStore(path, run_id="active-run")
    parsed = store.parsed_record_count

    headers = store.event_headers()

    assert store.parsed_record_count == parsed
    reference = _EagerEventStore(path, run_id="reference").read()
    assert [header.sequence for header in headers] == [event.sequence for event in reference]
    assert [header.type for header in headers] == [event.type for event in reference]
    assert [header.execution_id for header in headers] == [
        event.execution_id if event.HasField("execution_id") else None for event in reference
    ]


def test_attaching_to_a_large_log_does_not_parse_the_whole_log(tmp_path):  # noqa: ANN001, ANN201
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    count = 8 * _EAGER_TAIL_RECORDS
    _write_events(
        log_dir / "run-events.jsonl",
        _generated_events(seed=31, count=count, legacy_invocations=True),
    )

    parts = build_server_parts(log_dir)

    store = parts.journal._store  # noqa: SLF001  # accounting is the assertion
    assert store is not None
    # The eager tail, plus the SERVER_STARTED event attach records itself.
    assert store.parsed_record_count <= _EAGER_TAIL_RECORDS + 1
    assert store.parsed_record_count < count


def test_attach_indexes_legacy_lifecycle_identity_without_parsing_history(tmp_path):  # noqa: ANN001, ANN201
    """The header-driven index must reproduce the fully parsed one exactly."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    path = log_dir / "run-events.jsonl"
    events = _generated_events(
        seed=37, count=3 * _EAGER_TAIL_RECORDS, legacy_sequences=True, legacy_invocations=True
    )
    _write_events(path, events)

    parts = build_server_parts(log_dir)
    attached = parts.journal.read_history()

    reference = _EagerEventStore(path, run_id="reference").read()
    expected = _canonical_execution_events(reference)
    # Attaching appends its own SERVER_STARTED event on a fresh journal.
    assert _dump(attached[: len(expected)]) == _dump(expected)
    assert parts.journal._canonical_execution_ids == set()  # noqa: SLF001
    assert parts.journal._legacy_invocation_ids == {  # noqa: SLF001
        event.execution_id for event in reference if event.HasField("execution_id")
    }


def test_run_started_payload_is_readable_without_forcing_the_history(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    events = _generated_events(seed=41, count=4 * _EAGER_TAIL_RECORDS)
    events[0] = _event(
        1,
        ET.EVENT_TYPE_RUN_STARTED,
        data=events_pb2.RunStartedData(outer_loop="agent", input="objective", max_rounds=24),
    )
    _write_events(path, events)
    store = EventStore(path, run_id="active-run")
    parsed = store.parsed_record_count

    started = store.read_sequences(
        [
            header.sequence
            for header in store.event_headers()
            if header.type == ET.EVENT_TYPE_RUN_STARTED
        ]
    )

    assert started[0].WhichOneof("data") == "run_started"
    assert started[0].run_started.max_rounds == 24
    assert store.parsed_record_count == parsed + 1
