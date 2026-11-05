"""Result protocol 2 records: every valid fixture can be produced; what readers reject is refused.

The fixtures are the protocol's conformance corpus (``sdk/vs-evaluator/fixtures``),
and every stream written here is read back with the protocol's reference reader.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibesys_evaluator import Metric

# test-isolation: the record writer is the private half of main(); the corpus pins its output.
from vibesys_evaluator._private.records import Partial, RecordError, RecordStream
from vs_evaluator_protocol.api import parse_records, read_measurement

FIXTURES = Path(__file__).resolve().parents[3] / "vs-evaluator" / "fixtures"
VALID = sorted((FIXTURES / "valid").glob("*.jsonl"))


def _replay(lines: list[dict], path: Path) -> None:
    """Drive the writer with a fixture's records, as a benchmark would produce them."""
    stream = RecordStream(path)
    for record in lines:
        if record["kind"] == "hello":
            stream.hello(
                {
                    name: Metric(unit=spec.get("unit", ""), direction=spec.get("direction", "max"))
                    for name, spec in record["metrics"].items()
                }
            )
        elif record["kind"] == "result":
            stream.result(record["values"])
        else:
            partial = record.get("partial")
            stream.error(
                record["message"],
                partial=None
                if partial is None
                else Partial(
                    name=partial["name"],
                    value=partial["value"],
                    direction=partial["direction"],
                    unit=partial.get("unit", ""),
                ),
            )
            break  # an error ends the stream
    stream.close()


def _comparable(record: dict) -> dict:
    record = dict(record)
    if record["kind"] == "hello":
        # A spec without a direction is written with the default "max"; a required metric's
        # `required` key is omitted. Both mean the same to a reader.
        record["metrics"] = {
            n: {"unit": s.get("unit", ""), "direction": s.get("direction", "max")}
            for n, s in record["metrics"].items()
        }
    return record


@pytest.mark.parametrize("fixture", VALID, ids=lambda p: p.stem)
def test_every_valid_fixture_can_be_produced(fixture: Path, tmp_path: Path) -> None:
    lines = [json.loads(line) for line in fixture.read_text().splitlines() if line.strip()]
    if any(
        r["kind"] == "hello" and any("required" in s for s in r["metrics"].values()) for r in lines
    ):
        pytest.skip(
            "optional metrics are not part of the vibesys-evaluator API (every metric is required)"
        )
    if any(r.get("error_kind") for r in lines):
        pytest.skip("error kinds are not part of the vibesys-evaluator API")
    output = tmp_path / "out.jsonl"
    _replay(lines, output)
    written = [json.loads(line) for line in output.read_text().splitlines()]
    expected = lines[
        : next((n + 1 for n, r in enumerate(lines) if r["kind"] == "error"), len(lines))
    ]
    assert [_comparable(r) for r in written] == [_comparable(r) for r in expected]
    original = read_measurement(parse_records(fixture.read_text()))
    produced = read_measurement(parse_records(output.read_text()))
    assert (produced.values, produced.failure) == (original.values, original.failure)


def _stream(tmp_path: Path) -> RecordStream:
    stream = RecordStream(tmp_path / "out.jsonl")
    stream.hello(
        {"ops": Metric(unit="ops/s", direction="max"), "p99": Metric(unit="ms", direction="min")}
    )
    return stream


@pytest.mark.parametrize(
    ("values", "match"),
    [
        pytest.param({"ops": 1.0}, "did not report", id="missing-metric"),
        pytest.param({"ops": 1.0, "p99": 2.0, "p50": 1.0}, "declared only", id="unknown-metric"),
        pytest.param({"ops": 1.0, "p99": float("inf")}, "finite", id="non-finite-value"),
        pytest.param({"ops": True, "p99": 2.0}, "finite", id="boolean-value"),
        pytest.param({"ops": "fast", "p99": 2.0}, "finite", id="non-numeric-value"),
    ],
)
def test_a_result_a_reader_would_reject_is_refused(
    tmp_path: Path, values: dict, match: str
) -> None:
    with pytest.raises(RecordError, match=match):
        _stream(tmp_path).result(values)


def test_a_second_result_and_a_second_hello_are_refused(tmp_path: Path) -> None:
    stream = _stream(tmp_path)
    stream.result({"ops": 1.0, "p99": 2.0})
    with pytest.raises(RecordError, match="report once"):
        stream.result({"ops": 1.0, "p99": 2.0})
    with pytest.raises(RecordError, match="once, first"):
        stream.hello({"ops": Metric(unit="", direction="max")})


@pytest.mark.parametrize(
    "metrics",
    [
        {},
        {"p99 latency": Metric(unit="ms", direction="min")},
        {"": Metric(unit="", direction="min")},
    ],
    ids=["empty-metrics", "whitespace-metric-name", "empty-name"],
)
def test_a_hello_a_reader_would_reject_is_refused(tmp_path: Path, metrics: dict) -> None:
    with pytest.raises(RecordError):
        RecordStream(tmp_path / "out.jsonl").hello(metrics)


def test_a_result_before_hello_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RecordError, match="hello"):
        RecordStream(tmp_path / "out.jsonl").result({"ops": 1.0})


def test_an_error_after_a_result_ends_the_stream_as_failed(tmp_path: Path) -> None:
    stream = _stream(tmp_path)
    stream.result({"ops": 1.0, "p99": 2.0})
    stream.error("teardown failed", partial=Partial(name="ops_seen", value=3.0, direction="max"))
    stream.close()
    measurement = read_measurement(parse_records((tmp_path / "out.jsonl").read_text()))
    assert measurement.failure == "teardown failed"
    assert measurement.partial is not None


def test_a_blank_error_message_still_says_something(tmp_path: Path) -> None:
    stream = RecordStream(tmp_path / "out.jsonl")
    stream.error("  ")
    stream.close()
    assert read_measurement(parse_records((tmp_path / "out.jsonl").read_text())).failure


def test_a_long_error_message_is_cut(tmp_path: Path) -> None:
    stream = _stream(tmp_path)
    stream.error("x" * 10_000)
    stream.close()
    failure = read_measurement(parse_records((tmp_path / "out.jsonl").read_text())).failure
    assert failure is not None
    assert len(failure) <= 4000
