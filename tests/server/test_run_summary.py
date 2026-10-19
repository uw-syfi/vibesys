"""The run summary fold equals what a client folds of the same journal prefix, at every prefix."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from tests.server.run_summary_support import (
    ClientFold,
    client_fold,
    events,
    journal_lines,
)

from server.events import (
    EventStatus,
    EventType,
    GateFinishedData,
    RunEvent,
    SpanStartedData,
)
from server.run_summary import RunSummaryFold, schema_id
from vibesys.api import GateKind
from vs_core.api import Span, SpanLabel, SpanStatus, SpanType

if TYPE_CHECKING:
    from server.host_run_summary import SpanTokens, TurnReply, UsageRecord


def _size(document: object) -> int:
    return len(json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode())


def _summary_of(fold: RunSummaryFold) -> ClientFold:
    """The fold's state in the shape ``client_fold`` returns, replies with their schemas."""
    state = fold.state()
    return ClientFold(
        through_sequence=state.through_sequence,
        event_count=state.event_count,
        spans=state.spans,
        overview=state.overview,
        replies=tuple(
            {
                "span_id": reply.span_id,
                "kind": reply.kind,
                "json_schema": state.schemas[reply.schema_id],
                "value": reply.value,
            }
            for reply in state.replies
        ),
        tokens=state.tokens.model_dump(),
        usage={entry.span_id: entry.tally.model_dump() for entry in state.usage},
        input_gates=tuple(gate.model_dump(mode="json") for gate in state.input_gates),
        terminal=None if state.terminal is None else state.terminal.model_dump(mode="json"),
    )


@given(run_events=events())
def test_the_summary_equals_the_clients_fold_at_every_prefix(run_events: list) -> None:
    fold = RunSummaryFold()
    assert _summary_of(fold) == client_fold([])
    for count, line in enumerate(journal_lines(run_events), start=1):
        assert fold.consume(line)
        assert _summary_of(fold) == client_fold(run_events[:count])


@given(run_events=events(), cut=st.integers(min_value=0, max_value=40))
def test_continuing_a_state_equals_folding_the_whole_journal(run_events: list, cut: int) -> None:
    lines = journal_lines(run_events)
    cut = min(cut, len(lines))
    first = RunSummaryFold()
    for line in lines[:cut]:
        first.consume(line)

    resumed = RunSummaryFold.from_state(first.state())
    for line in lines[cut:]:
        resumed.consume(line)

    whole = RunSummaryFold()
    for line in lines:
        whole.consume(line)
    assert resumed.state() == whole.state()


@given(run_events=events(), cut=st.integers(min_value=1, max_value=4000))
def test_a_line_cut_short_is_no_event_when_it_may_be_unfinished(run_events: list, cut: int) -> None:
    fold = RunSummaryFold()
    for line in journal_lines(run_events):
        before = fold.state()
        torn = line.rstrip(b"\n")[:cut]
        if len(torn) < len(line.rstrip(b"\n")):
            assert not fold.consume(torn, complete=False)
            assert fold.state() == before
        assert fold.consume(line)


@given(run_events=events(max_size=60), budget=st.integers(min_value=1, max_value=900))
def test_pages_of_any_size_partition_the_summary_and_carry_each_schema_once(
    run_events: list, budget: int
) -> None:
    fold = RunSummaryFold()
    for line in journal_lines(run_events):
        fold.consume(line)
    state = fold.state()

    spans: list[dict] = []
    replies: list[TurnReply] = []
    usage: list[SpanTokens] = []
    records: list[UsageRecord] = []
    schemas: dict[str, dict] = {}
    offset: int | None = 0
    while offset is not None:
        page = fold.page(offset, budget)
        assert page.spans or page.replies or page.usage or page.records or page.next_offset is None
        for key in page.schemas:
            assert key not in schemas
        # a page carries the schema of every reply on it that no earlier reply uses
        known = set(schemas) | set(page.schemas)
        assert all(reply.schema_id in known for reply in page.replies)
        size = (
            sum(_size(span) for span in page.spans)
            + sum(_size(reply.value) for reply in page.replies)
            + sum(_size(schema) for schema in page.schemas.values())
            + sum(_size(entry.tally.model_dump()) for entry in page.usage)
            + sum(_size(record.model_dump(mode="json")) for record in page.records)
        )
        taken = len(page.spans) + len(page.replies) + len(page.usage) + len(page.records)
        assert taken == 1 or size <= budget
        spans.extend(page.spans)
        replies.extend(page.replies)
        usage.extend(page.usage)
        records.extend(page.records)
        schemas.update(page.schemas)
        assert page.next_offset is None or page.next_offset == (
            len(spans) + len(replies) + len(usage) + len(records)
        )
        offset = page.next_offset

    assert tuple(spans) == state.spans
    assert tuple(replies) == state.replies
    assert tuple(usage) == state.usage
    assert tuple(records) == state.records
    assert schemas == state.schemas
    assert fold.usage_count == len(usage)
    assert fold.record_count == len(records)
    assert fold.span_count == len(spans)
    assert fold.reply_count == len(replies)


def test_a_page_past_the_end_is_empty_and_ends_the_summary() -> None:
    page = RunSummaryFold().page(5)

    assert (page.spans, page.replies, page.schemas, page.usage, page.next_offset) == (
        (),
        (),
        {},
        (),
        None,
    )


def test_a_reply_schema_is_identified_by_its_canonical_json() -> None:
    assert schema_id({"b": 1, "a": [1, 2]}) == schema_id({"a": [1, 2], "b": 1})
    assert schema_id({"a": 1}) != schema_id({"a": 2})


def _gate(sequence: int, status: EventStatus, gate: GateKind = GateKind.BENCHMARK) -> RunEvent:
    return RunEvent(
        sequence=sequence,
        run_id="run",
        timestamp=datetime(2026, 10, 19, 12, tzinfo=UTC),
        type=EventType.GATE_FINISHED,
        data=GateFinishedData(
            gate=gate, output_tail="boom" if status is EventStatus.FAILED else None
        ),
        status=status,
    )


def _idea(sequence: int) -> RunEvent:
    return RunEvent(
        sequence=sequence,
        run_id="run",
        timestamp=datetime(2026, 10, 19, 12, tzinfo=UTC),
        type=EventType.SPAN_STARTED,
        data=SpanStartedData(
            span=Span(
                id="flow:search/idea#1",
                parent=None,
                type=SpanType.FLOW,
                name="idea",
                status=SpanStatus.RUNNING,
                started_at=1.0,
                label=SpanLabel(kind="workstream"),
            )
        ),
    )


def test_a_retry_of_a_failed_input_gate_that_passes_after_the_first_idea_is_kept() -> None:
    """The input benchmark failed, an idea began, and the gate's second attempt passed."""
    fold = RunSummaryFold()
    journal = [
        _gate(1, EventStatus.FAILED),
        _idea(2),
        _gate(3, EventStatus.COMPLETED, GateKind.ACCURACY),  # an idea's own gate: not kept
        _gate(4, EventStatus.COMPLETED),  # the retry of the failed gate
        _gate(5, EventStatus.COMPLETED),  # the gate no longer stands failed: not kept
        _gate(6, EventStatus.FAILED),  # an idea's failure: not kept
    ]
    for line in journal_lines(journal):
        assert fold.consume(line)
    assert [(gate.sequence, gate.passed) for gate in fold.state().input_gates] == [
        (1, False),
        (4, True),
    ]
