"""Reader behavior the shared fixture corpus does not pin down."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_evaluator_protocol.api import (
    PROTOCOL_VERSION,
    ErrorRecord,
    Hello,
    Measurement,
    MetricSpec,
    ProtocolError,
    ReasonCode,
    Result,
    check_objectives,
    parse_records,
    read_measurement,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_evaluator_protocol.api import Record

HELLO_LINE = (
    '{"kind":"hello","protocol":2,"metrics":{"throughput":{"unit":"ops/s","direction":"max"}}}'
)
RESULT_LINE = '{"kind":"result","label":"","values":{"throughput":41250.3}}'


def _hello(*names: str, optional: str | None = None) -> Hello:
    metrics = dict.fromkeys(names, MetricSpec())
    if optional is not None:
        metrics[optional] = MetricSpec(required=False)
    return Hello(protocol=PROTOCOL_VERSION, metrics=metrics)


def test_parse_records_skips_blank_lines_and_keeps_order() -> None:
    records = parse_records(f"\n{HELLO_LINE}\n\n{RESULT_LINE}\n")

    assert [type(record) for record in records] == [Hello, Result]
    assert records[0] == Hello(
        protocol=2,
        metrics={"throughput": MetricSpec(unit="ops/s", direction="max")},
    )


def test_parse_records_rejects_an_unknown_key_inside_a_metric_spec() -> None:
    line = '{"kind":"hello","protocol":2,"metrics":{"throughput":{"scale":"log"}}}'

    with pytest.raises(ProtocolError) as rejection:
        parse_records(line)

    assert rejection.value.code == ReasonCode.UNKNOWN_KEY
    assert "scale" in str(rejection.value)


@pytest.mark.parametrize("line", ["not json at all", '["kind","result"]'])
def test_parse_records_rejects_a_line_that_is_not_a_json_object(line: str) -> None:
    with pytest.raises(ProtocolError) as rejection:
        parse_records(f"{HELLO_LINE}\n{line}")

    assert rejection.value.code == ReasonCode.MALFORMED_LINE
    assert "line 2" in str(rejection.value)


def test_parse_records_rejects_a_record_without_a_kind() -> None:
    with pytest.raises(ProtocolError) as rejection:
        parse_records('{"protocol":2,"metrics":{}}')

    assert rejection.value.code == ReasonCode.UNKNOWN_KIND


def test_parse_records_rejects_a_coerced_field_type() -> None:
    with pytest.raises(ProtocolError) as rejection:
        parse_records('{"kind":"hello","protocol":"2","metrics":{"throughput":{}}}')

    assert rejection.value.code == ReasonCode.INVALID_RECORD
    assert "protocol" in str(rejection.value)


def test_read_measurement_consumes_a_lazy_record_stream() -> None:
    consumed: list[str] = []

    def stream() -> Iterator[Record]:
        for record in parse_records(f"{HELLO_LINE}\n{RESULT_LINE}"):
            consumed.append(record.kind)
            yield record

    measurement = read_measurement(stream())

    assert consumed == ["hello", "result"]
    assert measurement.values == {"throughput": 41250.3}
    assert measurement.metrics is not None
    assert measurement.metrics["throughput"].direction == "max"


def test_read_measurement_prefers_an_error_over_an_earlier_result() -> None:
    hello = _hello("throughput")
    records = [hello, Result(values={"throughput": 1.0}), ErrorRecord(message="runner died")]

    measurement = read_measurement(records)

    assert measurement.failed
    assert measurement.failure == "runner died"
    assert measurement.values is None
    assert measurement.metrics == hello.metrics


def test_read_measurement_accepts_an_error_without_a_result() -> None:
    measurement = read_measurement([_hello("throughput"), ErrorRecord(message="no run")])

    assert measurement.failure == "no run"


def test_read_measurement_rejects_a_second_result_record() -> None:
    records = [
        _hello("throughput"),
        Result(values={"throughput": 1.0}),
        Result(values={"throughput": 2.0}),
    ]

    with pytest.raises(ProtocolError) as rejection:
        read_measurement(records)

    assert rejection.value.code == ReasonCode.INVALID_RECORD
    assert "record 3" in str(rejection.value)


def test_read_measurement_accepts_a_declared_optional_metric_when_reported() -> None:
    records = [
        _hello("throughput", optional="p99_latency_ms"),
        Result(values={"throughput": 1.0, "p99_latency_ms": 812.0}),
    ]

    measurement = read_measurement(records)

    assert measurement.values == {"throughput": 1.0, "p99_latency_ms": 812.0}


def test_read_measurement_accepts_a_declared_optional_metric_when_absent() -> None:
    hello = _hello("throughput", optional="p99_latency_ms")

    measurement = read_measurement([hello, Result(values={"throughput": 1.0})])

    assert measurement.values == {"throughput": 1.0}
    assert measurement.metrics == hello.metrics


def test_read_measurement_rejects_a_row_missing_a_required_metric() -> None:
    records = [
        _hello("throughput", optional="p99_latency_ms"),
        Result(values={"p99_latency_ms": 812.0}),
    ]

    with pytest.raises(ProtocolError) as rejection:
        read_measurement(records)

    assert rejection.value.code == ReasonCode.MISSING_METRIC
    message = str(rejection.value)
    assert "'throughput'" in message
    assert "p99_latency_ms" not in message


def test_read_measurement_accepts_an_error_that_replaces_the_hello() -> None:
    measurement = read_measurement([ErrorRecord(message="config failed to parse")])

    assert measurement.failed
    assert measurement.failure == "config failed to parse"
    assert measurement.metrics is None
    assert measurement.values is None


def test_read_measurement_rejects_an_error_that_follows_a_record_without_a_hello() -> None:
    records = [Result(values={"throughput": 1.0}), ErrorRecord(message="runner died")]

    with pytest.raises(ProtocolError) as rejection:
        read_measurement(records)

    assert rejection.value.code == ReasonCode.MISSING_HELLO


def test_read_measurement_rejects_a_superseded_protocol_version() -> None:
    superseded = '{"kind":"hello","protocol":1,"metrics":{"throughput":{}}}'

    with pytest.raises(ProtocolError) as rejection:
        read_measurement(parse_records(f"{superseded}\n{RESULT_LINE}"))

    assert rejection.value.code == ReasonCode.UNSUPPORTED_PROTOCOL


def test_read_measurement_reports_every_undeclared_metric() -> None:
    records = [_hello("throughput"), Result(values={"throughput": 1.0, "latency_ms": 2.0})]

    with pytest.raises(ProtocolError) as rejection:
        read_measurement(records)

    assert rejection.value.code == ReasonCode.UNKNOWN_METRIC
    assert "latency_ms" in str(rejection.value)


def test_read_measurement_rejects_a_non_finite_value_parsed_from_json() -> None:
    stream = f'{HELLO_LINE}\n{{"kind":"result","label":"","values":{{"throughput":-1e999}}}}'

    with pytest.raises(ProtocolError) as rejection:
        read_measurement(parse_records(stream))

    assert rejection.value.code == ReasonCode.NON_FINITE_VALUE
    assert "throughput" in str(rejection.value)


def test_read_measurement_rejects_a_boolean_before_treating_it_as_a_number() -> None:
    records = [_hello("throughput"), Result(values={"throughput": True})]

    with pytest.raises(ProtocolError) as rejection:
        read_measurement(records)

    assert rejection.value.code == ReasonCode.NON_NUMERIC_VALUE


def test_read_measurement_widens_integer_values_to_float() -> None:
    measurement = read_measurement([_hello("enqueued"), Result(values={"enqueued": 7})])

    assert measurement.values == {"enqueued": 7.0}
    assert isinstance(next(iter((measurement.values or {}).values())), float)


def test_measurement_rejects_an_outcome_that_is_both_or_neither() -> None:
    metrics = {"throughput": MetricSpec()}

    with pytest.raises(ValueError, match="either values or a failure"):
        Measurement(metrics=metrics)
    with pytest.raises(ValueError, match="either values or a failure"):
        Measurement(metrics=metrics, values={"throughput": 1.0}, failure="runner died")


def test_measurement_rejects_a_row_without_the_declaration_it_was_validated_against() -> None:
    with pytest.raises(ValueError, match="carries the metric declaration"):
        Measurement(metrics=None, values={"throughput": 1.0})


def test_check_objectives_accepts_a_subset_of_the_declared_metrics() -> None:
    hello = _hello("throughput", "latency_ms")

    assert check_objectives(hello, {"throughput"}) is None
    assert check_objectives(hello, set()) is None


def test_check_objectives_names_the_missing_objective_and_the_declared_metrics() -> None:
    hello = _hello("throughput", "latency_ms")

    with pytest.raises(ProtocolError) as rejection:
        check_objectives(hello, {"throughput", "goodput"})

    assert rejection.value.code == ReasonCode.MISSING_METRIC
    message = str(rejection.value)
    assert "'goodput'" in message
    assert "'throughput'" in message
    assert "'latency_ms'" in message


def test_check_objectives_rejects_an_objective_declared_optional() -> None:
    hello = _hello("throughput", optional="p99_latency_ms")

    with pytest.raises(ProtocolError) as rejection:
        check_objectives(hello, {"p99_latency_ms"})

    assert rejection.value.code == ReasonCode.MISSING_METRIC
    message = str(rejection.value)
    assert "'p99_latency_ms'" in message
    assert "optional" in message
    assert "not declared" not in message


def test_check_objectives_separates_undeclared_objectives_from_optional_ones() -> None:
    hello = _hello("throughput", optional="p99_latency_ms")

    with pytest.raises(ProtocolError) as rejection:
        check_objectives(hello, {"goodput", "p99_latency_ms", "throughput"})

    message = str(rejection.value)
    assert "objectives 'goodput' are not declared" in message
    assert "objectives 'p99_latency_ms' are declared optional" in message
