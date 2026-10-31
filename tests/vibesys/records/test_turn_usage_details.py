"""A finished turn's per-model usage reaches the run's typed records, whole and validated.

The journal's ``turn_usage_recorded`` carries a turn's models, cost basis and provider session id;
the projector writes them to the span's ``usage_details``, one entry per turn, beside the
main-conversation ``usage`` the span has always held. A line without them adds no entry, so a
journal recorded before the split projects to the bytes it always did.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

from hypothesis import given, settings
from tests.support.usage_events import split_usage_lines, usage_line

import server.events as wire
import vibesys.events as e
import vs_proto.api as p
from server.host_run_summary import RecordedCostBasis, UsageSpeedName
from vibesys.records.api import MappedEvent, MappingState, map_event
from vs_agent.api import CostBasis, UsageSpeed
from vs_sim.api.testing import examples

_AT = datetime(2026, 1, 1, tzinfo=UTC)
_ROOT = Path(__file__).resolve().parents[3]


def _turn(line: dict[str, Any], *, sequence: int = 1) -> e.CoreEvent:
    fields = {"timestamp": _AT, "run_id": "run-a", "execution_id": "inv-1", "agent_kind": "judge"}
    return e.CoreEvent.model_validate(
        {
            **fields,
            "type": e.CoreEventType.TURN_USAGE_RECORDED,
            "sequence": sequence,
            "data": line["data"],
        }
    )


def _attrs(result: MappedEvent) -> p.AgentTurnAttrs:
    for record in result.records:
        assert record.body is not None
        if record.body.field == "span":
            span = record.body.value.span
            assert span is not None
            assert span.label is not None
            assert span.label.attrs is not None
            return p.AgentTurnAttrs.from_binary(span.label.attrs.value)
    message = "no span record"
    raise AssertionError(message)


def _project(*lines: dict[str, Any]) -> p.AgentTurnAttrs:
    state = MappingState("run-a")
    result = map_event(state, _turn(lines[0]))
    for sequence, line in enumerate(lines[1:], start=2):
        result = map_event(result.state, _turn(line, sequence=sequence))
    for record in result.records:
        p.validate(record)
    return _attrs(result)


@settings(max_examples=examples(pr=60, nightly=1500))
@given(line=split_usage_lines())
def test_a_turns_rows_basis_and_session_are_projected_as_the_journal_wrote_them(
    line: dict[str, Any],
) -> None:
    data = line["data"]

    (detail,) = _project(line).usage_details

    session = detail.provider_session_id if detail.has_field("provider_session_id") else None
    assert session == data["provider_session_id"]
    assert (None if not detail.has_field("cost_basis") else detail.cost_basis.name.lower()) == data[
        "cost_basis"
    ]
    assert len(detail.by_model) == len(data["by_model"])
    for row, given_row in zip(detail.by_model, data["by_model"], strict=True):
        assert (
            row.model,
            row.input,
            row.cache_read,
            row.cache_creation,
            row.cache_creation_1h,
            row.output,
            row.reasoning,
            row.turns,
            row.primary,
        ) == (
            given_row["model"],
            given_row["input_tokens"],
            given_row["cache_read_input_tokens"],
            given_row["cache_creation_input_tokens"],
            given_row["cache_creation_1h_input_tokens"],
            given_row["output_tokens"],
            given_row["reasoning_output_tokens"],
            given_row["turns"],
            given_row["primary"],
        )
        assert (row.recorded_usd if row.has_field("recorded_usd") else None) == given_row[
            "recorded_usd"
        ]
        assert (row.speed.name.lower() if row.has_field("speed") else None) == given_row["speed"]


@settings(max_examples=examples(pr=40, nightly=800))
@given(line=split_usage_lines())
def test_the_projected_detail_survives_the_wire_and_validates(line: dict[str, Any]) -> None:
    attrs = _project(line)

    again = p.AgentTurnAttrs.from_binary(attrs.to_binary())

    p.validate(again)
    assert again.usage_details == attrs.usage_details


def test_a_line_recorded_before_the_split_adds_no_detail_and_keeps_the_span_unchanged() -> None:
    old = usage_line(
        {"input_tokens": 5, "output_tokens": 2, "total_cost_usd": 0.25, "duration_ms": 10}
    )

    attrs = _project(old)

    assert attrs.usage_details == []
    assert attrs.usage == p.TokenUsage(input=5, output=2, cost_usd=0.25, duration_ms=10)


@settings(max_examples=examples(pr=30, nightly=600))
@given(first=split_usage_lines(), second=split_usage_lines())
def test_each_turn_of_a_span_adds_its_own_detail_in_order_and_the_main_totals_still_sum(
    first: dict[str, Any], second: dict[str, Any]
) -> None:
    attrs = _project(first, second)

    assert [detail.provider_session_id or None for detail in attrs.usage_details] == [
        first["data"]["provider_session_id"],
        second["data"]["provider_session_id"],
    ]
    assert attrs.usage is not None
    assert attrs.usage.input == first["data"]["input_tokens"] + second["data"]["input_tokens"]
    # Each turn has exactly one primary row, so two turns of one span have two.
    assert sum(row.primary for detail in attrs.usage_details for row in detail.by_model) == 2


def test_a_turn_with_only_a_session_id_still_names_its_conversation() -> None:
    attrs = _project(usage_line({"input_tokens": 1, "provider_session_id": "thread-9"}))

    (detail,) = attrs.usage_details
    assert detail.provider_session_id == "thread-9"
    assert detail.by_model == []
    assert not detail.has_field("cost_basis")


def test_the_journal_and_the_proto_name_the_same_bases_and_speeds() -> None:
    """One vocabulary written four ways: the library's enum, the journal's literals, the proto's enum."""
    proto_bases = {m.name.lower() for m in p.UsageCostBasis if m.value != 0}
    recorded = {member.value for member in CostBasis}
    wire_bases = set(
        get_args(get_args(wire.TurnUsageData.model_fields["cost_basis"].annotation)[0])
    )
    assert recorded == wire_bases == set(get_args(RecordedCostBasis))
    assert recorded < proto_bases
    assert proto_bases - recorded == {"legacy_recorded", "legacy_unpriced"}

    proto_speeds = {m.name.lower() for m in p.UsageSpeed if m.value != 0}
    wire_speeds = set(
        get_args(get_args(wire.ModelTurnUsageData.model_fields["speed"].annotation)[0])
    )
    assert {member.value for member in UsageSpeed} == proto_speeds == wire_speeds
    assert proto_speeds == set(get_args(UsageSpeedName))


def test_the_corpus_event_projects_to_the_detail_the_wire_example_describes() -> None:
    corpus = json.loads(
        (_ROOT / "tests/conformance/events/turn_usage_recorded.json").read_text(encoding="utf-8")
    )

    (detail,) = _project(corpus).usage_details

    assert detail.provider_session_id == "claude-session-1"
    assert detail.cost_basis == p.UsageCostBasis.PRICE_TABLE
    assert [(row.model, row.primary, row.speed) for row in detail.by_model] == [
        ("claude-opus-5-5", True, p.UsageSpeed.FAST)
    ]
