"""The partial measurement a failed run may report on its `error` record."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_evaluator_protocol.api import (
    PROTOCOL_VERSION,
    ErrorRecord,
    Hello,
    MetricSpec,
    PartialMeasurement,
    Progress,
    ProtocolError,
    ReasonCode,
    parse_records,
    read_measurement,
)

_NAMES = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Zs", "Zl", "Zp", "Cc")),
    min_size=1,
    max_size=24,
)
_FINITE = st.floats(allow_nan=False, allow_infinity=False)
_PROGRESS = st.builds(
    Progress,
    completed=st.integers(min_value=0, max_value=10**6),
    required=st.integers(min_value=1, max_value=10**6),
    unit=_NAMES,
)
_PARTIALS = st.builds(
    PartialMeasurement,
    name=_NAMES,
    value=_FINITE,
    direction=st.sampled_from(["max", "min"]),
    unit=st.none() | st.text(min_size=1, max_size=24),
    target=st.none() | _FINITE,
    progress=st.none() | _PROGRESS,
)
_HELLO = Hello(protocol=PROTOCOL_VERSION, metrics={"throughput": MetricSpec(direction="max")})


def _stream(*records: ErrorRecord | Hello) -> str:
    return "".join(record.model_dump_json(exclude_none=True) + "\n" for record in records)


@given(partial=_PARTIALS, declared=st.booleans())
def test_a_partial_measurement_reaches_the_reader_unchanged(
    partial: PartialMeasurement, *, declared: bool
) -> None:
    error = ErrorRecord(message="warmup did not finish", partial=partial)
    stream = _stream(_HELLO, error) if declared else _stream(error)

    measurement = read_measurement(parse_records(stream))

    assert measurement.failed
    assert measurement.partial == partial


@given(declared=st.booleans())
def test_an_error_without_a_partial_measurement_reports_none(*, declared: bool) -> None:
    error = ErrorRecord(message="server crashed")
    stream = _stream(_HELLO, error) if declared else _stream(error)

    assert read_measurement(parse_records(stream)).partial is None


@pytest.mark.parametrize(
    ("partial", "key"),
    [
        ({"name": "rate", "value": 1, "direction": "max", "speed": 2}, "partial.speed"),
        (
            {
                "name": "rate",
                "value": 1,
                "direction": "max",
                "progress": {"completed": 1, "required": 2, "unit": "rounds", "eta": 3},
            },
            "partial.progress.eta",
        ),
    ],
)
def test_an_unknown_key_in_a_partial_measurement_is_rejected_by_name(
    partial: dict[str, object], key: str
) -> None:
    line = json.dumps({"kind": "error", "message": "stopped", "partial": partial})

    with pytest.raises(ProtocolError) as rejection:
        parse_records(line)

    assert rejection.value.code == ReasonCode.UNKNOWN_KEY
    assert repr(key) in str(rejection.value)


@pytest.mark.parametrize(
    ("partial", "key"),
    [
        ({"name": "rate", "value": 1}, "partial.direction"),
        ({"name": "rate", "value": True, "direction": "max"}, "partial.value"),
        ({"name": "rate", "value": "1", "direction": "max"}, "partial.value"),
        ({"name": "two words", "value": 1, "direction": "max"}, "partial.name"),
        ({"name": "rate", "value": 1, "direction": "up"}, "partial.direction"),
        (
            {
                "name": "rate",
                "value": 1,
                "direction": "max",
                "progress": {"completed": 1, "required": 0, "unit": "rounds"},
            },
            "partial.progress.required",
        ),
    ],
)
def test_an_invalid_partial_measurement_is_rejected_by_key(
    partial: dict[str, object], key: str
) -> None:
    line = json.dumps({"kind": "error", "message": "stopped", "partial": partial})

    with pytest.raises(ProtocolError) as rejection:
        parse_records(line)

    assert rejection.value.code == ReasonCode.INVALID_RECORD
    assert repr(key) in str(rejection.value)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_partial_value_is_rejected(value: str) -> None:
    line = (
        '{"kind":"error","message":"stopped","partial":'
        f'{{"name":"rate","value":{value},"direction":"max"}}}}'
    )

    with pytest.raises(ProtocolError) as rejection:
        parse_records(line)

    assert rejection.value.code == ReasonCode.INVALID_RECORD
