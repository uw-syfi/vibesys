"""Every encoding the gate schema accepts retains its readings at the writer boundary."""

import json
from datetime import UTC, datetime
from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vibesys.events as e
import vs_proto.api as p
from vibesys.records.api import MappingState, map_event


def gate(
    span_id: str,
    parent: str | None,
    value: float,
    direction: Literal["max", "min"],
    *,
    scalar: bool,
) -> e.CoreEvent:
    rows = [{"name": "speed", "value": value, "unit": "ops/s", "direction": direction}]
    data = e.SpanEndedData.model_validate(
        {
            "span": {
                "id": span_id,
                "parent": parent,
                "type": "operation",
                "name": "measure",
                "status": "done",
                "started_at": 1,
                "ended_at": 2,
                "label": {
                    "kind": "gate",
                    "verdict": "passed",
                    "attrs_json": json.dumps(
                        {
                            "stage": "build,accuracy,benchmark",
                            "metric_value": value,
                            "metrics": [] if scalar else rows,
                        }
                    ),
                },
            }
        }
    )
    return event(e.CoreEventType.SPAN_ENDED, data)


def event(kind: e.CoreEventType, data: e.CoreEventData) -> e.CoreEvent:
    return e.CoreEvent(
        type=kind, data=data, timestamp=datetime(2026, 1, 1, tzinfo=UTC), run_id="run-a"
    )


@pytest.mark.regression
@example(scalar=True, direction="max", input_value=100, values=[517])
@given(
    scalar=st.booleans(),
    direction=st.sampled_from(["max", "min"]),
    input_value=st.integers(1, 1000),
    values=st.lists(st.integers(1, 10000), min_size=1, max_size=4),
)
def test_scalar_and_vector_gate_readings_reach_the_same_store_series(
    *, scalar: bool, direction: Literal["max", "min"], input_value: int, values: list[int]
) -> None:
    started = event(
        e.CoreEventType.RUN_STARTED,
        e.RunStartedData(
            outer_loop="dynamic",
            input="input",
            overview=e.RunOverview(
                objective=e.ObjectiveOverview(
                    text="Speed", metric="speed", unit="ops/s", direction=direction
                )
            ),
        ),
    )
    owner = e.SpanStartedData.model_validate(
        {
            "span": {
                "id": "flow:idea",
                "parent": None,
                "type": "flow",
                "name": "idea",
                "status": "running",
                "started_at": 0,
                "label": {"kind": "workstream", "attrs_json": "{}"},
            }
        }
    )
    observations = [
        started,
        event(e.CoreEventType.SPAN_STARTED, owner),
        gate("op:input", None, input_value, direction, scalar=False),
    ]
    observations += [
        gate(f"op:measure-{index}", "flow:idea", value, direction, scalar=scalar)
        for index, value in enumerate(values)
    ]
    state = MappingState("run-a")
    readings = {}
    for sequence, observation in enumerate(observations, 1):
        mapped = map_event(state, observation.model_copy(update={"sequence": sequence}))
        state = mapped.state
        for record in mapped.records:
            if record.body is not None and record.body.field == "metric":
                point = record.body.value
                readings[point.span] = (point.series, point.value)
    assert readings == {
        "op:input": ("speed", input_value),
        **{f"op:measure-{index}": ("speed", value) for index, value in enumerate(values)},
    }
    assert state.input_measurement is not None
    fact = p.InputMeasurement.from_binary(state.input_measurement)
    assert fact.state is not None
    assert fact.state.field == "measured"
    assert fact.state.value.value == input_value


@pytest.mark.regression
@example(base=1, delta=1, scalar=False, direction="max", input_gate=False)
@given(
    base=st.integers(1, 1000),
    delta=st.integers(1, 1000),
    scalar=st.booleans(),
    direction=st.sampled_from(["max", "min"]),
    input_gate=st.booleans(),
)
def test_individual_samples_and_settled_mean_have_distinct_identities(
    base: int, delta: int, *, scalar: bool, direction: Literal["max", "min"], input_gate: bool
) -> None:
    """A repeat's last sample must not claim the aggregate operation's immutable reading."""
    started = event(
        e.CoreEventType.RUN_STARTED,
        e.RunStartedData(
            outer_loop="dynamic",
            input="input",
            overview=e.RunOverview(
                objective=e.ObjectiveOverview(text="Speed", metric="speed", direction=direction)
            ),
        ),
    )
    parent = None if input_gate else "flow:idea"
    mean = base + delta / 2
    aggregate = gate("op:measure", parent, mean, direction, scalar=scalar)
    assert isinstance(aggregate.data, e.SpanEndedData)
    pending = e.SpanStartedData.model_validate(
        {
            "span": {
                **aggregate.data.span.model_dump(),
                "status": "running",
                "ended_at": None,
                "label": None,
            }
        }
    ).span
    observations = [started]
    if not input_gate:
        owner = {
            **pending.model_dump(),
            "id": parent,
            "parent": None,
            "label": {"kind": "workstream", "attrs_json": "{}"},
        }
        observations.append(
            event(e.CoreEventType.SPAN_STARTED, e.SpanStartedData.model_validate({"span": owner}))
        )
    observations.append(event(e.CoreEventType.SPAN_STARTED, e.SpanStartedData(span=pending)))
    for index, value in enumerate((base, base + delta)):
        observations.append(
            event(
                e.CoreEventType.GATE_FINISHED,
                e.GateFinishedData(
                    gate=e.GateKind.BENCHMARK, span_id="op:measure", metric="speed", value=value
                ),
            ).model_copy(update={"execution_id": f"evaluation-{index}"})
        )
    observations.append(aggregate)
    state = MappingState("run-a")
    readings = {}
    for sequence, observation in enumerate(observations, 1):
        mapped = map_event(state, observation.model_copy(update={"sequence": sequence}))
        state = mapped.state
        for record in mapped.records:
            p.validate(record)
            if record.body is not None and record.body.field == "metric":
                point = record.body.value
                readings[point.span] = point.value
    assert readings.pop("op:measure") == mean
    assert sorted(readings.values()) == [base, base + delta]
    for span_id in readings:
        assert p.Span.from_binary(dict(state.spans)[span_id]).parent == "op:measure"
    if input_gate:
        assert state.input_measurement is not None
        fact = p.InputMeasurement.from_binary(state.input_measurement)
        assert fact.state is not None
        assert fact.state.field == "measured"
        assert fact.state.value.value == mean

    last_sample = observations[-2]
    assert isinstance(last_sample.data, e.GateFinishedData)
    changed_sample = last_sample.model_copy(
        update={
            "sequence": len(observations) + 1,
            "data": last_sample.data.model_copy(update={"value": base + delta + 1}),
        }
    )
    with pytest.raises(ValueError, match="cannot become"):
        map_event(state, changed_sample)
