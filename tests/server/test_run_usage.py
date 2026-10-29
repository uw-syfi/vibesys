"""The usage records of a journal: one per finished turn, joined to the execution that ran it."""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from tests.server.run_summary_support import events, journal_lines

from server.events import RunEvent
from server.host_run_summary import TokenTally, UsageRecord
from server.run_summary import RunSummaryFold
from server.run_summary_cache import summarize_journal
from server.run_usage import usage_records

if TYPE_CHECKING:
    from collections.abc import Sequence

_FIXTURES = Path(__file__).parents[1] / "conformance" / "fixtures" / "usage-records"
_NAMES = sorted(path.name for path in _FIXTURES.glob("*.json"))


def _documents(run_events: Sequence[RunEvent]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in journal_lines(run_events)]


def _plus(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    return a if b is None else a + b


def _sum_tokens(records: Sequence[UsageRecord]) -> dict[str, int | None]:
    total: dict[str, int | None] = dict.fromkeys(TokenTally.model_fields)
    for record in records:
        for key, value in record.tokens.model_dump().items():
            total[key] = _plus(total[key], value)
    return total


@given(run_events=events(max_size=80))
def test_record_sums_equal_the_summary_totals_and_every_record_names_its_executions_role_and_harness(
    run_events: list[RunEvent],
) -> None:
    fold = RunSummaryFold()
    for line in journal_lines(run_events):
        fold.consume(line)

    records = usage_records(_documents(run_events))

    assert records == list(fold.state().records)
    assert _sum_tokens(records) == fold.tokens.model_dump()
    assert fold.record_count == len(records)

    started: dict[str, RunEvent] = {}
    expected_costs: list[float] = []
    expected_turns: list[tuple[str | None, str | None, str | None]] = []
    last = 0
    for event in run_events:
        if event.sequence <= last:
            continue
        last = event.sequence
        if event.type.value == "agent_execution_started" and event.execution_id is not None:
            started[event.execution_id] = event
        elif event.type.value == "turn_usage_recorded":
            begun = started.get(event.execution_id or "")
            provider = None if begun is None else getattr(begun.data, "provider", None)
            expected_turns.append(
                (None if begun is None else begun.agent_kind, provider, event.execution_id)
            )
            cost = getattr(event.data, "total_cost_usd", None)
            if cost is not None:
                expected_costs.append(cost)

    assert [(r.role, r.harness, r.execution_id) for r in records] == expected_turns
    reported = [r.cost_usd for r in records if r.cost_usd is not None]
    assert math.isclose(sum(reported), sum(expected_costs), abs_tol=1e-9)


@given(run_events=events(max_size=60))
def test_the_projection_is_the_same_whether_read_whole_or_through_a_continued_state(
    run_events: list[RunEvent],
) -> None:
    lines = journal_lines(run_events)
    cut = len(lines) // 2
    first = RunSummaryFold()
    for line in lines[:cut]:
        first.consume(line)
    resumed = RunSummaryFold.from_state(first.state())
    for line in lines[cut:]:
        resumed.consume(line)

    assert list(resumed.state().records) == usage_records(_documents(run_events))


def test_a_turn_whose_execution_start_is_missing_still_yields_a_record_with_no_role_or_harness() -> (
    None
):
    usage = {
        "sequence": 1,
        "run_id": "run-a",
        "timestamp": "2026-10-10T12:00:00Z",
        "type": "turn_usage_recorded",
        "agent_kind": "implementer",
        "execution_id": "inv:implementer:000001:0",
        "data": {"kind": "turn_usage_recorded", "input_tokens": 7, "total_cost_usd": 0.25},
    }

    [record] = usage_records([usage])

    assert (record.role, record.harness, record.model, record.started_at) == (
        None,
        None,
        None,
        None,
    )
    assert record.reasoning_effort is None
    assert record.harness_version is None
    assert (record.run_id, record.execution_id, record.tokens.input) == (
        "run-a",
        "inv:implementer:000001:0",
        7,
    )
    assert record.tokens.output is None
    assert record.cost_usd == 0.25
    assert record.ended_at is not None


def test_a_turn_with_no_execution_id_yields_a_record_with_no_execution() -> None:
    usage = {
        "sequence": 1,
        "timestamp": "2026-10-10T12:00:00Z",
        "type": "turn_usage_recorded",
        "data": {"kind": "turn_usage_recorded"},
    }

    [record] = usage_records([usage])

    assert record.execution_id is None
    assert record.run_id is None
    assert record.tokens == TokenTally(
        input=None, cache_read=None, cache_write=None, output=None, reasoning=None
    )


@pytest.mark.parametrize("name", _NAMES)
def test_the_projection_of_a_fixture_journal_is_its_records(name: str) -> None:
    fixture = json.loads((_FIXTURES / name).read_text())
    for event in fixture["events"]:
        RunEvent.model_validate(event)  # the fixture is a journal a run could have recorded
    journal = b"".join(
        json.dumps(event, separators=(",", ":")).encode() + b"\n" for event in fixture["events"]
    )

    projected = [r.model_dump(mode="json") for r in usage_records(fixture["events"])]
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "run-events.jsonl"
        path.write_bytes(journal)
        cold = summarize_journal(path)
        warm = summarize_journal(path)  # from the cache the first read wrote

    assert projected == fixture["records"]
    for summarized in (cold, warm):
        assert [r.model_dump(mode="json") for r in summarized.fold.state().records] == projected
