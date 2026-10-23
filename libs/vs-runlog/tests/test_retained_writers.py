"""Saved outputs of preceding writers retain their meaning at the store read boundary."""

import io
import json
from pathlib import Path
from shutil import copytree

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_proto.api import (
    EpochStarted,
    InputMeasurement,
    Keyframe,
    MeasuredInput,
    MetricPoint,
    Oneof,
    RunHead,
    UnmeasuredInput,
    ValidationError,
    validate,
)
from vs_runlog.api import RunLogReader, fold_skeleton, read_run_head, upgrade_skeleton

CORPUS = Path(__file__).resolve().parents[3] / "proto/testdata/retained"
CASES = json.loads((CORPUS / "manifest.json").read_text())


@pytest.mark.parametrize("version", CASES)
def test_saved_previous_writers_reopen_with_the_same_facts(tmp_path: Path, version: str) -> None:
    source = CORPUS / version
    directory = tmp_path / version
    copytree(source, directory)
    original = (directory / "run.head").read_bytes()
    raw = RunHead.from_binary(original)
    assert raw.objective is not None
    assert raw.objective.has_field("input_value") is not CASES[version]["head_input_absent"]
    head = read_run_head(directory / "run.log")
    assert head.objective is not None
    assert head.objective.input_value == CASES[version]["input"]
    with (directory / "run.log").open("rb") as stream:
        state = upgrade_skeleton(
            fold_skeleton(RunLogReader(stream), log_offset=(directory / "run.log").stat().st_size)
        )
    for path in (directory / "keyframes").glob("*.pb"):
        cached = upgrade_skeleton(Keyframe.from_binary(path.read_bytes()))
        assert cached.epochs == [v for v in state.epochs if v.seq <= cached.through_seq]
    fact = input_fact(state)
    assert fact is not None
    assert fact.state is not None
    assert fact.state.field == "measured"
    assert fact.state.value.value == CASES[version]["input"]
    validate(state)
    assert (directory / "run.head").read_bytes() == original
    assert upgrade_skeleton(state) == state


@pytest.mark.regression
@example(spanless=False, value=100.0, explicit=False, split=18)
@example(spanless=True, value=0.0, explicit=False, split=14)
@given(
    spanless=st.booleans(),
    value=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
    explicit=st.booleans(),
    split=st.integers(1, 18),
)
def test_input_upgrades_preserve_prefixes_and_explicit_facts(
    *, spanless: bool, value: float, explicit: bool, split: int
) -> None:
    """Both old input encodings, signed values, and explicit unmeasured declarations, at every seek."""
    records = list(RunLogReader(io.BytesIO((CORPUS / next(iter(CASES)) / "run.log").read_bytes())))
    measured_seq = 0
    for record in records:
        body = record.body
        if body is None:
            continue
        if isinstance(body.value, EpochStarted) and explicit:
            assert body.value.run is not None
            body.value.run.input_measurement = InputMeasurement(
                state=Oneof("unmeasured", UnmeasuredInput())
            )
        elif body.field == "metric" and body.value.span == "input-gate":
            body.value.value = value
            if spanless:
                record.body = Oneof(
                    "metric",
                    MetricPoint(series=body.value.series, value=value, clock=body.value.clock),
                )
            measured_seq = record.seq
    assert measured_seq > 0
    whole = upgrade_skeleton(fold_skeleton(records, log_offset=0))
    partial = upgrade_skeleton(fold_skeleton(records[:split], log_offset=0))
    assert [v for v in whole.epochs if v.seq <= split] == partial.epochs
    latest = input_fact(partial)
    if explicit:
        assert latest is not None
        assert latest.state is not None
        assert latest.state.field == "unmeasured"
    elif split >= measured_seq:
        assert latest is not None
        assert latest.state is not None
        assert latest.state.field == "measured"
        assert latest.state.value.value == value
    else:
        assert latest is None


def input_fact(state: Keyframe) -> InputMeasurement | None:
    epoch = state.epochs[-1].epoch
    assert epoch is not None
    assert epoch.run is not None
    return epoch.run.input_measurement


@pytest.mark.parametrize("spanless", [False, True])
def test_record_upgrade_keeps_the_metric_and_validates_its_declaration(*, spanless: bool) -> None:
    records = list(RunLogReader(io.BytesIO((CORPUS / next(iter(CASES)) / "run.log").read_bytes())))
    record = records[13]
    assert record.body is not None
    assert record.body.field == "metric"
    point = record.body.value
    if spanless:
        point = MetricPoint(series=point.series, value=point.value, clock=point.clock)
        record.body = Oneof("metric", point)
    record.input_measurement = InputMeasurement(
        state=Oneof(
            "measured", MeasuredInput(series=point.series, value=point.value, span=point.span)
        )
    )
    validate(record)
    state = fold_skeleton(records, log_offset=8)
    fact = input_fact(state)
    assert fact == record.input_measurement
    assert state.metrics[0].points[-1].point == point
    record.input_measurement = InputMeasurement(
        state=Oneof(
            "measured", MeasuredInput(series=point.series, value=point.value + 1, span=point.span)
        )
    )
    with pytest.raises(ValidationError):
        validate(record)
