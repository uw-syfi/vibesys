"""Equivalence and accounting tests for the lazily attached event store.

The store indexes a run log with a cheap scan and validates records only when
a read reaches them. That is an optimization, so the contract under test is
equivalence: for any log, the lazy store must hand out exactly the events the
fully eager path would, at every cursor and every bounded range.
"""

import random
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import pytest
from pydantic import ValidationError
from tests.server.support import build_server_parts

from server.events import (
    _EAGER_TAIL_RECORDS,
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    EventStore,
    EventType,
    InvocationFinishedData,
    InvocationStartedData,
    OutputData,
    RoundFinishedData,
    RunEvent,
    RunStartedData,
    _records_from_events,
    _repair_legacy_sequences,
    make_event,
)
from server.journal import _canonical_execution_events

_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)


class _EagerEventStore(EventStore):
    """The reference implementation: never trust the scan, always validate.

    This is the real fallback the store takes on any doubt, so the equivalence
    tests compare the two production paths against each other.
    """

    def _scan_unlocked(self):  # noqa: ANN202
        events, malformed_tail_offset = self._read_unlocked()
        return _records_from_events(_repair_legacy_sequences(events)), malformed_tail_offset


def _write_events(path: Path, events: list[RunEvent]) -> None:
    path.write_text("".join(event.model_dump_json() + "\n" for event in events))


def _dump(events: list[RunEvent]) -> list[str]:
    return [event.model_dump_json() for event in events]


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


class _CommonFields(TypedDict):
    """The ``RunEvent`` fields every generated event shares, spread as kwargs."""

    sequence: int
    run_id: str
    timestamp: datetime


def _generated_events(
    seed: int, count: int, *, legacy_sequences: bool = False, legacy_invocations: bool = False
) -> list[RunEvent]:
    """Build a deterministic log mixing the event shapes a real run writes."""
    rng = random.Random(seed)  # noqa: S311  # deterministic fixture data, not crypto
    plan = _legacy_sequence_plan(count) if legacy_sequences else list(range(1, count + 1))
    events: list[RunEvent] = []
    open_execution: str | None = None
    for index, sequence in enumerate(plan):
        common: _CommonFields = {
            "sequence": sequence,
            "run_id": "persisted-run",
            "timestamp": _TIMESTAMP,
        }
        roll = rng.random()
        if roll < 0.06:
            open_execution = f"exec-{index}"
            started = (
                EventType.INVOCATION_STARTED
                if legacy_invocations
                else EventType.AGENT_EXECUTION_STARTED
            )
            data = (
                InvocationStartedData(system_prompt="sys", user_prompt=f"prompt-{index}")
                if legacy_invocations
                else AgentExecutionStartedData(
                    stage="implementer",
                    user_prompt=f"prompt-{index}",
                    activity=AgentExecutionActivityData(mode="thinking", summary="Thinking"),
                )
            )
            events.append(
                RunEvent(
                    **common,
                    type=started,
                    agent_kind="implementer",
                    round_label=f"round-{index}",
                    execution_id=open_execution,
                    data=data,
                )
            )
        elif roll < 0.12 and open_execution is not None:
            finished = (
                EventType.INVOCATION_FINISHED
                if legacy_invocations
                else EventType.AGENT_EXECUTION_FINISHED
            )
            data = (
                InvocationFinishedData(result={"ok": True})
                if legacy_invocations
                else AgentExecutionFinishedData(result={"ok": True})
            )
            events.append(
                RunEvent(
                    **common,
                    type=finished,
                    agent_kind="implementer",
                    round_label=f"round-{index}",
                    execution_id=open_execution,
                    data=data,
                )
            )
            open_execution = None
        elif roll < 0.15:
            events.append(
                RunEvent(
                    **common,
                    type=EventType.ROUND_FINISHED,
                    round_label=f"round-{index}",
                    data=RoundFinishedData(attempts=1, judge_verdict="pass"),
                )
            )
        else:
            content = "x" * rng.randint(1, 400)
            events.append(
                RunEvent(
                    **common,
                    type=EventType.OUTPUT,
                    text=f"line-{index}",
                    data=OutputData(stream="stdout", content=content),
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
    lines = [event.model_dump_json() for event in events]
    lines[17] = "not-json"
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ValidationError):
        EventStore(path, run_id="active-run")
    with pytest.raises(ValidationError):
        _EagerEventStore(path, run_id="reference")


def test_a_corrupt_final_record_is_ignored_and_repaired_by_append(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    events = _generated_events(seed=5, count=3 * _EAGER_TAIL_RECORDS)
    path.write_text(
        "".join(event.model_dump_json() + "\n" for event in events) + '{"protocol_version":1'
    )

    store = EventStore(path, run_id="active-run")
    assert _dump(store.read()) == _dump(_EagerEventStore(path, run_id="reference").read())

    appended = store.append(make_event(EventType.OUTPUT, "after repair"))
    reopened = EventStore(path, run_id="reopened-run")

    assert [event.text for event in reopened.read(appended.sequence - 1)] == ["after repair"]
    assert len(reopened.read()) == len(events) + 1


def test_lazy_store_matches_eager_store_for_a_valid_unterminated_tail(tmp_path):  # noqa: ANN001, ANN201
    serialized = "".join(
        event.model_dump_json() + "\n" for event in _generated_events(seed=43, count=12)
    )
    path = tmp_path / "events.jsonl"
    path.write_text(serialized.rstrip("\n"))

    _assert_matches_eager_store(path)

    lazy_path = tmp_path / "lazy.jsonl"
    eager_path = tmp_path / "eager.jsonl"
    lazy_path.write_text(serialized.rstrip("\n"))
    eager_path.write_text(serialized.rstrip("\n"))
    appended = make_event(EventType.OUTPUT, "after repair")
    EventStore(lazy_path, run_id="reference").append(appended)
    _EagerEventStore(eager_path, run_id="reference").append(appended)

    assert lazy_path.read_bytes() == eager_path.read_bytes()
    # The repair terminates the final record instead of concatenating onto it.
    assert lazy_path.read_bytes().startswith(serialized.encode())


def test_lazy_store_matches_eager_store_for_a_complete_invalid_tail(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    invalid = (
        '{"protocol_version":1,"sequence":99,"run_id":"persisted-run",'
        '"timestamp":"2026-01-01T00:00:00+00:00","type":"output",'
        '"data":{"kind":"output","stream":"stdout","content":5}}'
    )
    path.write_text(
        "".join(event.model_dump_json() + "\n" for event in _generated_events(seed=47, count=12))
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
    serialized = [event.model_dump_json() for event in _generated_events(seed=53, count=12)]
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
    # Frozen events are shared, not copied, so the same objects come back.
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
        event.execution_id for event in reference
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
        event.execution_id for event in reference if event.execution_id is not None
    }


def test_run_started_payload_is_readable_without_forcing_the_history(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "events.jsonl"
    events = _generated_events(seed=41, count=4 * _EAGER_TAIL_RECORDS)
    events[0] = RunEvent(
        sequence=1,
        run_id="persisted-run",
        timestamp=_TIMESTAMP,
        type=EventType.RUN_STARTED,
        data=RunStartedData(outer_loop="agent", input="objective", max_rounds=24),
    )
    _write_events(path, events)
    store = EventStore(path, run_id="active-run")
    parsed = store.parsed_record_count

    started = store.read_sequences(
        [
            header.sequence
            for header in store.event_headers()
            if header.type is EventType.RUN_STARTED
        ]
    )

    assert isinstance(started[0].data, RunStartedData)
    assert started[0].data.max_rounds == 24
    assert store.parsed_record_count == parsed + 1
