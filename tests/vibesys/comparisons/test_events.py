"""Comparison events retain every stored reading, including hidden baselines."""

from hypothesis import example, given
from hypothesis import strategies as st

from vibesys.comparisons.api import ComparisonReport, comparisons_read_data
from vs_runtime.api.comparisons import (
    ComparisonFailed,
    ComparisonMeasured,
    ComparisonNotConfigured,
    ComparisonNotMeasured,
    ComparisonSample,
)

_TEXT = st.text(alphabet="abc XYZ0123", min_size=1, max_size=20)
_READINGS = st.one_of(
    st.builds(
        ComparisonMeasured,
        metric=_TEXT,
        unit=st.one_of(st.none(), _TEXT),
        direction=st.sampled_from([None, "min", "max"]),
        samples=st.lists(
            st.builds(
                ComparisonSample,
                value=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
                host=st.one_of(st.none(), _TEXT),
            ),
            min_size=1,
            max_size=4,
        ).map(tuple),
    ),
    st.builds(ComparisonFailed, reason=_TEXT),
    st.builds(ComparisonNotConfigured, reason=_TEXT),
    st.builds(ComparisonNotMeasured),
)


@given(
    reports=st.lists(
        st.builds(
            ComparisonReport,
            name=_TEXT,
            label=_TEXT,
            reading=_READINGS,
            cached=st.booleans(),
            visible_to_agents=st.booleans(),
        ),
        max_size=6,
    )
)
@example(reports=[])
@example(
    reports=[
        ComparisonReport(
            "measured",
            "Hidden baseline",
            ComparisonMeasured(
                metric="throughput",
                unit="items/s",
                direction="max",
                samples=(ComparisonSample(value=10, host="node-a"), ComparisonSample(value=12)),
            ),
        ),
        ComparisonReport("failed", "Failed baseline", ComparisonFailed(reason="benchmark failed")),
        ComparisonReport(
            "unconfigured", "Unconfigured baseline", ComparisonNotConfigured(reason="no cluster")
        ),
        ComparisonReport("unmeasured", "Unmeasured baseline", ComparisonNotMeasured()),
    ]
)
def test_comparison_events_preserve_readings_and_task_order(
    reports: list[ComparisonReport],
) -> None:
    payload = comparisons_read_data(iter(reports))

    assert payload.kind == "comparisons_read"
    assert [(row.name, row.label, row.status.value) for row in payload.comparisons] == [
        (report.name, report.label, report.reading.status) for report in reports
    ]
    assert type(payload).model_validate_json(payload.model_dump_json()) == payload
    for row, report in zip(payload.comparisons, reports, strict=True):
        reading = report.reading
        if isinstance(reading, ComparisonMeasured):
            assert (row.metric, row.unit, row.direction) == (
                reading.metric,
                reading.unit,
                reading.direction,
            )
            assert (row.value, row.spread, row.samples, row.hosts) == (
                reading.value,
                reading.spread,
                len(reading.samples),
                reading.hosts,
            )
            assert row.reason is None
        else:
            assert (row.metric, row.unit, row.direction, row.value, row.spread, row.samples) == (
                None,
            ) * 6
            assert row.hosts == ()
            assert row.reason == (
                reading.reason
                if isinstance(reading, ComparisonFailed | ComparisonNotConfigured)
                else None
            )
