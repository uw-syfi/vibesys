"""The kind an `error` record may carry: whether measuring again could help."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_evaluator_protocol.api import (
    ErrorKind,
    ErrorRecord,
    ProtocolError,
    ReasonCode,
    parse_records,
    read_measurement,
)

_REQUIREMENTS = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Zs", "Zl", "Zp", "Cc")),
    min_size=1,
    max_size=24,
)


def _line(**keys: object) -> str:
    return json.dumps({"kind": "error", "message": "no cache hits", **keys}) + "\n"


@given(requirement=_REQUIREMENTS)
def test_a_requirement_not_met_failure_reaches_the_reader_with_its_requirement(
    requirement: str,
) -> None:
    text = _line(error_kind="requirement_not_met", requirement=requirement)

    measurement = read_measurement(parse_records(text))

    assert measurement.failed
    assert measurement.error_kind is ErrorKind.REQUIREMENT_NOT_MET
    assert measurement.requirement == requirement


def test_an_error_without_a_kind_is_transient() -> None:
    """The protocol's backward-compatible default: records written before kinds existed."""
    measurement = read_measurement(parse_records(_line()))

    assert measurement.failed
    assert measurement.error_kind is ErrorKind.TRANSIENT
    assert measurement.requirement is None


def test_a_transient_kind_named_outright_reads_as_the_default() -> None:
    named = read_measurement(parse_records(_line(error_kind="transient")))

    assert named == read_measurement(parse_records(_line()))


def test_a_measured_row_carries_no_error_kind() -> None:
    hello = '{"kind":"hello","protocol":2,"metrics":{"m":{}}}\n'
    result = '{"kind":"result","values":{"m":1.0}}\n'

    measurement = read_measurement(parse_records(hello + result))

    assert measurement.error_kind is None
    assert measurement.requirement is None


@pytest.mark.parametrize(
    ("keys", "key"),
    [
        ({"error_kind": "flaky"}, "error_kind"),
        ({"error_kind": 3}, "error_kind"),
        ({"error_kind": "requirement_not_met"}, "requirement"),
        ({"error_kind": "transient", "requirement": "prefix_cache"}, "requirement"),
        ({"requirement": "prefix_cache"}, "requirement"),
        ({"error_kind": "requirement_not_met", "requirement": ""}, "requirement"),
        ({"error_kind": "requirement_not_met", "requirement": "prefix cache"}, "requirement"),
        ({"error_kind": "requirement_not_met", "requirement": "x", "kind_of": 1}, "kind_of"),
    ],
)
def test_a_malformed_kind_is_rejected_naming_the_key(keys: dict[str, object], key: str) -> None:
    with pytest.raises(ProtocolError) as rejection:
        parse_records(_line(**keys))

    assert rejection.value.code in (ReasonCode.INVALID_RECORD, ReasonCode.UNKNOWN_KEY)
    assert key in str(rejection.value)


def test_the_record_model_refuses_what_the_wire_refuses() -> None:
    with pytest.raises(ValueError, match="requirement"):
        ErrorRecord(message="m", error_kind=ErrorKind.REQUIREMENT_NOT_MET)
