"""A run journals the comparison readings it found, in the task's order, and the wire accepts them."""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter

from headless.framework_events import format_framework_event
from server.events import EventData
from vibesys.comparisons.api import ComparisonReport, comparisons_read_data
from vibesys.events import ComparisonStatus, CoreEventType, EventStatus, make_core_event
from vs_runtime.api.comparisons import (
    ComparisonFailed,
    ComparisonMeasured,
    ComparisonNotConfigured,
    ComparisonNotMeasured,
    ComparisonSample,
)

_FLOATS = st.floats(min_value=0.001, max_value=1e6, allow_nan=False, allow_infinity=False)
_LABELS = st.text(alphabet="abcdefghijklmnop ", min_size=1, max_size=12).map(str.strip).filter(bool)
_READINGS = st.one_of(
    st.lists(_FLOATS, min_size=1, max_size=4).map(
        lambda values: ComparisonMeasured(
            metric="tok",
            unit="tok/s",
            direction="max",
            samples=tuple(ComparisonSample(value=v, host=f"n{i}") for i, v in enumerate(values)),
        )
    ),
    st.just(ComparisonFailed(reason="the server never answered")),
    st.just(ComparisonNotConfigured(reason="no machine settings")),
    st.just(ComparisonNotMeasured()),
)
_REPORTS = st.lists(
    st.builds(
        ComparisonReport,
        name=st.just("c"),
        label=_LABELS,
        reading=_READINGS,
        cached=st.booleans(),
        visible_to_agents=st.booleans(),
    ),
    max_size=4,
)

_STATUS = {
    ComparisonMeasured: ComparisonStatus.MEASURED,
    ComparisonFailed: ComparisonStatus.FAILED,
    ComparisonNotConfigured: ComparisonStatus.NOT_CONFIGURED,
    ComparisonNotMeasured: ComparisonStatus.NOT_MEASURED,
}


@given(reports=_REPORTS)
def test_the_event_has_one_row_per_comparison_in_order_and_the_wire_accepts_it(
    reports: list[ComparisonReport],
) -> None:
    data = comparisons_read_data(reports)

    assert [row.label for row in data.comparisons] == [r.label for r in reports]
    assert [row.status for row in data.comparisons] == [_STATUS[type(r.reading)] for r in reports]
    for row, report in zip(data.comparisons, reports, strict=True):
        if isinstance(report.reading, ComparisonMeasured):
            assert row.value == report.reading.value
            assert row.samples == len(report.reading.samples)
            assert (row.spread is None) == (len(report.reading.samples) < 2)
        else:
            assert row.value is None
    wire = TypeAdapter(EventData).validate_python(data.model_dump(mode="json"))
    assert wire.kind == "comparisons_read"


def test_the_terminal_shows_each_baseline_as_a_value_or_a_status_word() -> None:
    measured = ComparisonMeasured(
        metric="tok", unit="tok/s", direction="max", samples=(ComparisonSample(value=66.2),)
    )
    data = comparisons_read_data(
        [
            ComparisonReport("v", "vLLM", measured),
            ComparisonReport("s", "SGLang", ComparisonNotMeasured()),
        ]
    )
    event = make_core_event(
        CoreEventType.COMPARISONS_READ,
        data=data,
        status=EventStatus.COMPLETED,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert format_framework_event(event) == "[baselines] vLLM 66.2 tok/s, SGLang not measured"
