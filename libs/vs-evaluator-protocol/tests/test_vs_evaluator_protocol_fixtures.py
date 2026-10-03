"""Conformance of the reader against the shared protocol fixture corpus."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vs_evaluator_protocol.api import (
    Hello,
    Measurement,
    ProtocolError,
    ReasonCode,
    parse_records,
    read_measurement,
)


def _fixtures_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "sdk" / "vs-evaluator" / "fixtures"
        if candidate.is_dir():
            return candidate
    pytest.fail("sdk/vs-evaluator/fixtures not found above this test file")


FIXTURES = _fixtures_root()
EXPECTATIONS: dict[str, dict[str, Any]] = json.loads(
    (FIXTURES / "expectations.json").read_text(encoding="utf-8")
)
INVALID_STREAMS = sorted((FIXTURES / "invalid").glob("*.jsonl"))
VALID_STREAMS = sorted((FIXTURES / "valid").glob("*.jsonl"))


def _read(stream: Path) -> Measurement:
    return read_measurement(parse_records(stream.read_text(encoding="utf-8")))


def test_every_fixture_has_an_expectation() -> None:
    assert {path.stem for path in INVALID_STREAMS} == set(EXPECTATIONS["invalid"])
    assert {path.stem for path in VALID_STREAMS} == set(EXPECTATIONS["valid"])


@pytest.mark.parametrize("stream", INVALID_STREAMS, ids=lambda path: path.stem)
def test_invalid_stream_is_rejected_with_the_contract_reason_code(stream: Path) -> None:
    with pytest.raises(ProtocolError) as rejection:
        _read(stream)

    assert rejection.value.code == ReasonCode(EXPECTATIONS["invalid"][stream.stem])
    assert str(rejection.value)


@pytest.mark.parametrize("stream", VALID_STREAMS, ids=lambda path: path.stem)
def test_valid_stream_reads_the_expected_outcome(stream: Path) -> None:
    expected = EXPECTATIONS["valid"][stream.stem]

    measurement = _read(stream)

    if expected["outcome"] == "result":
        assert not measurement.failed
        assert measurement.failure is None
        assert measurement.values == expected["values"]
        assert measurement.metrics is not None
        reported = set(expected["values"])
        declared = set(measurement.metrics)
        required = {name for name, spec in measurement.metrics.items() if spec.required}
        assert reported <= declared
        assert required <= reported
    else:
        assert measurement.failed
        assert measurement.values is None
        assert measurement.failure
        # A failed stream carries the declaration only when it had one: an
        # error may arrive with no preceding hello.
        records = parse_records(stream.read_text(encoding="utf-8"))
        declares = any(isinstance(record, Hello) for record in records)
        assert (measurement.metrics is not None) == declares
