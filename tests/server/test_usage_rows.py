"""A turn's usage rows: one per model, keyed on how the cost was reached, requests counted once.

``usage_rows`` is what a reader prices. These properties hold it to the contract the Usage screen
relies on: rows of any number of turns can be added up and priced per model, speed and basis
without counting a turn twice or losing a token, and a turn recorded before the split existed still
reads, through the one function that knows the old shape.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.support.usage_events import BASES, split_usage_lines, usage_line

import vs_proto.api as p
from server.run_usage import UsageCostBasis, basis_of, legacy_basis, usage_records, usage_rows
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from server.host_run_summary import UsageRecord


def _record(event: dict[str, Any]) -> UsageRecord:
    (record,) = usage_records([event])
    return record


@settings(max_examples=examples(pr=60, nightly=1500))
@given(event=split_usage_lines())
def test_a_split_turn_has_one_row_per_model_with_one_primary_that_counts_the_requests(
    event: dict[str, Any],
) -> None:
    record = _record(event)
    rows = usage_rows(record)
    given_rows = event["data"]["by_model"]

    assert [row.model for row in rows] == [each["model"] for each in given_rows]
    assert sum(row.primary for row in rows) == 1
    primary = next(row for row in rows if row.primary)
    assert (
        sum(row.turns for row in rows)
        == primary.turns
        == next(each["turns"] for each in given_rows if each["primary"])
    )
    assert all(row.turns == 0 for row in rows if not row.primary)


@settings(max_examples=examples(pr=60, nightly=1500))
@given(event=split_usage_lines())
def test_the_rows_of_a_turn_sum_to_the_whole_turn_in_every_token_and_in_recorded_cost(
    event: dict[str, Any],
) -> None:
    rows = usage_rows(_record(event))
    given_rows = event["data"]["by_model"]

    sums = {
        "input": "input_tokens",
        "cache_read": "cache_read_input_tokens",
        "cache_write": "cache_creation_input_tokens",
        "output": "output_tokens",
        "reasoning": "reasoning_output_tokens",
    }
    for field, journal_name in sums.items():
        assert sum(getattr(row.tokens, field) for row in rows) == sum(
            each[journal_name] for each in given_rows
        )
    assert sum(row.cache_write_1h or 0 for row in rows) == sum(
        each["cache_creation_1h_input_tokens"] for each in given_rows
    )
    priced = [each["recorded_usd"] for each in given_rows if each["recorded_usd"] is not None]
    assert math.isclose(
        math.fsum(row.recorded_usd for row in rows if row.recorded_usd is not None),
        math.fsum(priced),
    )


@settings(max_examples=examples(pr=60, nightly=1500))
@given(event=split_usage_lines())
def test_every_row_of_a_turn_has_the_turns_basis_and_a_reported_speed_reads_as_standard(
    event: dict[str, Any],
) -> None:
    record = _record(event)
    rows = usage_rows(record)

    assert {row.basis for row in rows} == {basis_of(record)}
    assert [row.speed for row in rows] == [
        each["speed"] or "standard" for each in event["data"]["by_model"]
    ]


@settings(max_examples=examples(pr=40, nightly=800))
@given(events=st.lists(split_usage_lines(), min_size=1, max_size=6))
def test_requests_are_counted_once_however_many_turns_and_models_are_added_up(
    events: list[dict[str, Any]],
) -> None:
    rows = [
        row
        for sequence, event in enumerate(events, start=1)
        for row in usage_rows(_record({**event, "sequence": sequence}))
    ]

    assert sum(row.turns for row in rows) == sum(
        each["turns"] for event in events for each in event["data"]["by_model"] if each["primary"]
    )
    assert sum(row.primary for row in rows) == len(events)


def test_a_turn_with_no_split_is_one_row_of_its_own_tokens_and_cost_that_counts_one_request() -> (
    None
):
    event = usage_line(
        {
            "input_tokens": 10,
            "cache_read_input_tokens": 4,
            "output_tokens": 2,
            "total_cost_usd": 0.5,
            "cost_basis": "provider_reported",
        }
    )
    record = _record(event)

    (row,) = usage_rows(record)

    assert (row.tokens, row.recorded_usd, row.turns, row.primary) == (
        record.tokens,
        0.5,
        1,
        True,
    )
    assert row.basis is UsageCostBasis.PROVIDER_REPORTED


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"total_cost_usd": 0.5}, UsageCostBasis.LEGACY_RECORDED),
        ({"total_cost_usd": 0.0}, UsageCostBasis.LEGACY_RECORDED),
        ({"input_tokens": 3}, UsageCostBasis.LEGACY_UNPRICED),
        ({}, UsageCostBasis.LEGACY_UNPRICED),
    ],
)
def test_a_turn_recorded_before_the_basis_existed_reads_by_whether_it_has_a_cost(
    data: dict[str, Any], expected: UsageCostBasis
) -> None:
    record = _record(usage_line(data))

    assert legacy_basis(record) is expected
    assert basis_of(record) is expected
    assert [row.basis for row in usage_rows(record)] == [expected]


@settings(max_examples=examples(pr=60, nightly=1000))
@given(
    basis=st.sampled_from(BASES),
    cost=st.none() | st.floats(0, 1000, allow_nan=False),
)
def test_a_recorded_basis_is_never_replaced_by_a_legacy_one(basis: str, cost: float | None) -> None:
    record = _record(usage_line({"cost_basis": basis, "total_cost_usd": cost}))

    assert basis_of(record) is UsageCostBasis(basis)
    assert all(row.basis is UsageCostBasis(basis) for row in usage_rows(record))


_UNPRICED_ROW = {
    "model": "gpt-0-unpriced",
    "input_tokens": 10,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_creation_1h_input_tokens": 0,
    "output_tokens": 1,
    "reasoning_output_tokens": 0,
    "turns": 1,
    "recorded_usd": None,
    "speed": None,
    "primary": True,
}


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(
            {"cost_basis": "provider_reported", "total_cost_usd": 0.5},
            UsageCostBasis.PROVIDER_REPORTED,
            id="opencode reports a cost",
        ),
        pytest.param(
            {"cost_basis": "not_reported", "input_tokens": 3},
            UsageCostBasis.NOT_REPORTED,
            id="gemini reports none",
        ),
        pytest.param(
            {"cost_basis": "unknown_model", "by_model": [_UNPRICED_ROW]},
            UsageCostBasis.UNKNOWN_MODEL,
            id="codex on a model with no price",
        ),
        pytest.param(
            {"cost_basis": "not_reported", "total_cost_usd": 0.5},
            UsageCostBasis.NOT_REPORTED,
            id="a cost the library gave no source for",
        ),
    ],
)
def test_a_new_line_is_never_read_as_an_old_one_whatever_its_provider_reported(
    data: dict[str, Any], expected: UsageCostBasis
) -> None:
    """Each of these once recorded a null basis, which reads as a line from before the field."""
    record = _record(usage_line(data))

    assert basis_of(record) is expected
    assert all(row.basis is expected for row in usage_rows(record))
    assert expected not in {UsageCostBasis.LEGACY_RECORDED, UsageCostBasis.LEGACY_UNPRICED}


def test_the_basis_vocabulary_is_the_protos_by_name() -> None:
    proto = {member.name for member in p.UsageCostBasis if member != p.UsageCostBasis.UNSPECIFIED}

    assert {member.name for member in UsageCostBasis} == proto


@settings(max_examples=examples(pr=40, nightly=600))
@given(
    event=split_usage_lines(),
    index=st.integers(0, 3),
    broken=st.sampled_from(
        [
            ("model", ""),
            ("model", None),
            ("input_tokens", -1),
            ("input_tokens", 1.5),
            ("input_tokens", True),
            ("input_tokens", None),
            ("turns", "1"),
            ("primary", 1),
            ("primary", None),
        ]
    ),
)
@example(
    event=usage_line(
        {
            "total_cost_usd": 0.5,
            "by_model": [
                {
                    "model": "a",
                    "input_tokens": 1,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_creation_1h_input_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 0,
                    "turns": 1,
                    "recorded_usd": None,
                    "speed": None,
                    "primary": True,
                }
            ],
        }
    ),
    index=0,
    broken=("model", ""),
)
def test_a_malformed_row_makes_the_whole_split_unreported_and_keeps_the_rest_of_the_turn(
    event: dict[str, Any], index: int, broken: tuple[str, object]
) -> None:
    rows = event["data"]["by_model"]
    rows[index % len(rows)][broken[0]] = broken[1]

    record = _record(event)

    assert record.by_model == ()
    assert record.cost_usd == event["data"].get("total_cost_usd")
    assert record.tokens.input == event["data"].get("input_tokens")
    assert [row.turns for row in usage_rows(record)] == [1]


@pytest.mark.parametrize("value", ["", "legacy_recorded", "PRICE_TABLE", 3, None])
def test_a_basis_the_journal_does_not_name_is_not_recorded(value: object) -> None:
    record = _record(usage_line({"cost_basis": value, "total_cost_usd": 0.1}))

    assert record.cost_basis is None
    assert basis_of(record) is UsageCostBasis.LEGACY_RECORDED
