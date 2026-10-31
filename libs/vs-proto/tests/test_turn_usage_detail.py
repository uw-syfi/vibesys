"""A finished turn's per-model usage validates at the public boundary, and its invariants hold.

``TurnUsageDetail`` and ``ModelTurnUsage`` are the contract of the usage split; the journal's
Pydantic fields mirror them until the journal retires. Their rules are the library's own
invariants, so a row the library builds always passes and one that breaks them is refused by name.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from protovalidate import validate as schema_validate

import vs_proto.api as p

if TYPE_CHECKING:
    from collections.abc import Callable

_COUNT = st.integers(min_value=0, max_value=10**9)


@st.composite
def _rows(draw: st.DrawFn, *, primary: bool) -> p.ModelTurnUsage:
    """A row under the library's invariants: cache within input, reasoning within output."""
    input_tokens = draw(_COUNT)
    output = draw(_COUNT)
    cache_read = draw(st.integers(0, input_tokens))
    cache_creation = draw(st.integers(0, input_tokens - cache_read))
    return p.ModelTurnUsage(
        model=draw(st.text(min_size=1, max_size=24)),
        input=input_tokens,
        cache_read=cache_read,
        cache_creation=cache_creation,
        cache_creation_1h=draw(st.integers(0, cache_creation)),
        output=output,
        reasoning=draw(st.integers(0, output)),
        turns=draw(st.integers(1, 1000)) if primary else 0,
        recorded_usd=draw(st.none() | st.floats(0, 1e6, allow_nan=False, allow_infinity=False)),
        speed=draw(st.none() | st.sampled_from([s for s in p.UsageSpeed if s.value != 0])),
        primary=primary,
    )


@st.composite
def _details(draw: st.DrawFn) -> p.TurnUsageDetail:
    size = draw(st.integers(0, 4))
    primary = draw(st.integers(0, max(size - 1, 0)))
    rows = [draw(_rows(primary=index == primary)) for index in range(size)]
    return p.TurnUsageDetail(
        provider_session_id=draw(st.none() | st.text(min_size=1, max_size=24)),
        cost_basis=draw(
            st.none()
            | st.sampled_from(
                [
                    p.UsageCostBasis.PRICE_TABLE,
                    p.UsageCostBasis.PROVIDER_REPORTED,
                    p.UsageCostBasis.UNKNOWN_MODEL,
                    p.UsageCostBasis.NOT_REPORTED,
                ]
            )
        ),
        by_model=rows,
    )


def _row(**changes: str | int | float | bool | p.UsageSpeed) -> p.ModelTurnUsage:
    """The smallest valid primary row, with ``changes`` set on it."""
    row = p.ModelTurnUsage(model="m", input=10, turns=1, primary=True)
    for name, value in changes.items():
        setattr(row, name, value)
    return row


def _invalid(message: p.TurnUsageDetail | p.ModelTurnUsage, fragment: str) -> None:
    """Refused, and the refusal names ``fragment`` in its message, rule id or field path."""
    with pytest.raises(p.ValidationError) as failure:
        p.validate(message)
    violations = failure.value.to_proto().violations
    assert any(
        fragment in violation.message
        or fragment in violation.rule_id
        or (
            violation.field is not None
            and fragment in [element.field_name for element in violation.field.elements]
        )
        for violation in violations
    ), violations


@given(detail=_details())
def test_every_detail_the_library_could_build_is_valid_and_survives_the_wire(
    detail: p.TurnUsageDetail,
) -> None:
    p.validate(detail)
    assert p.TurnUsageDetail.from_binary(detail.to_binary()) == detail


@given(detail=_details())
def test_a_detail_with_rows_has_exactly_one_primary_and_counts_requests_on_it_alone(
    detail: p.TurnUsageDetail,
) -> None:
    rows = detail.by_model
    assert sum(row.primary for row in rows) == (1 if rows else 0)
    assert all(row.turns == 0 for row in rows if not row.primary)


@given(detail=_details())
def test_the_protos_own_schema_agrees_with_the_generated_validator(
    detail: p.TurnUsageDetail,
) -> None:
    """The rules live in the ``.proto``; the retained schema, not a copy, decides validity."""
    schema_validate(detail)


def test_a_turn_with_no_rows_is_valid_and_absence_is_unreported() -> None:
    detail = p.TurnUsageDetail.from_binary(p.TurnUsageDetail().to_binary())

    p.validate(detail)
    assert not detail.has_field("cost_basis")
    assert not detail.has_field("provider_session_id")
    assert detail.by_model == []


@pytest.mark.parametrize(
    ("make", "fragment"),
    [
        pytest.param(
            lambda: p.TurnUsageDetail(by_model=[_row(), _row(model="n")]),
            "exactly one primary",
            id="two primaries",
        ),
        pytest.param(
            lambda: p.TurnUsageDetail(by_model=[_row(primary=False, turns=0)]),
            "exactly one primary",
            id="no primary",
        ),
        pytest.param(
            lambda: p.TurnUsageDetail(by_model=[_row(), _row(model="n", primary=False, turns=2)]),
            "turns",
            id="requests on a row that is not the primary",
        ),
        pytest.param(
            lambda: p.TurnUsageDetail(cost_basis=p.UsageCostBasis.LEGACY_RECORDED),
            "cost_basis",
            id="a legacy basis is never recorded",
        ),
        pytest.param(
            lambda: p.TurnUsageDetail(cost_basis=p.UsageCostBasis.LEGACY_UNPRICED),
            "cost_basis",
            id="the other legacy basis",
        ),
        pytest.param(
            lambda: p.TurnUsageDetail(cost_basis=p.UsageCostBasis.UNSPECIFIED),
            "cost_basis",
            id="an unspecified basis is absence, spelled by omitting the field",
        ),
        pytest.param(
            lambda: p.TurnUsageDetail(provider_session_id=""),
            "provider_session_id",
            id="an empty session id",
        ),
        pytest.param(lambda: _row(model=""), "model", id="no model name"),
        pytest.param(
            lambda: _row(input=5, cache_read=3, cache_creation=3),
            "cache reads and writes",
            id="cache beyond the input",
        ),
        pytest.param(
            lambda: _row(cache_creation=2, cache_creation_1h=3),
            "one-hour cache writes",
            id="one-hour writes beyond the writes",
        ),
        pytest.param(
            lambda: _row(output=2, reasoning=3),
            "reasoning is part of the output",
            id="reasoning beyond the output",
        ),
        pytest.param(lambda: _row(recorded_usd=-0.01), "recorded_usd", id="a negative price"),
        pytest.param(
            lambda: _row(recorded_usd=math.nan), "recorded_usd", id="a price that is not a number"
        ),
        pytest.param(lambda: _row(recorded_usd=math.inf), "recorded_usd", id="an infinite price"),
        pytest.param(
            lambda: _row(speed=p.UsageSpeed.UNSPECIFIED), "speed", id="an unspecified speed"
        ),
    ],
)
def test_a_row_or_detail_that_breaks_the_librarys_invariants_is_refused_by_name(
    make: Callable[[], p.TurnUsageDetail | p.ModelTurnUsage], fragment: str
) -> None:
    _invalid(make(), fragment)


def test_the_vocabulary_is_closed_over_the_members_the_journal_writes() -> None:
    assert {m.name for m in p.UsageSpeed} == {"UNSPECIFIED", "STANDARD", "FAST", "ULTRAFAST"}
    assert {m.name for m in p.UsageCostBasis} == {
        "UNSPECIFIED",
        "PRICE_TABLE",
        "PROVIDER_REPORTED",
        "UNKNOWN_MODEL",
        "LEGACY_RECORDED",
        "LEGACY_UNPRICED",
        "NOT_REPORTED",
    }
