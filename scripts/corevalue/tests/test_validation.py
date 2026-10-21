"""Differential properties of the compiler, independent of run scheduling contracts."""

from __future__ import annotations

import json

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf import Oneof
from pydantic import ValidationError

from scripts.corevalue.tests.api import (
    Choice,
    EnumSelection,
    ExtendedQuantity,
    OrderedValues,
    Ordinal,
    Quantity,
    decode_value,
    encode_value,
)
from scripts.corevalue.tests.api import (
    compiler_values as pb,
)
from vs_core.api import values
from vs_proto.api import ValidationError as ProtoValidationError
from vs_proto.api import core_agent_facts, core_common, core_vocabulary
from vs_runtime.api.core_values import from_proto, to_proto


@given(
    positions=st.lists(st.integers(min_value=1, max_value=10), max_size=10),
    size=st.integers(min_value=0, max_value=10),
)
@example(positions=[2, 1], size=2)
@example(positions=[1, 1], size=1)
@example(positions=[2**100], size=1)
def test_ordered_decimal_collections_agree_in_constructor_json_and_wire(
    positions: list[int], size: int
) -> None:
    coverage = tuple(Ordinal(ordinal=i + 1) for i in range(size))
    records = tuple(Ordinal(ordinal=i) for i in positions)
    accepted = positions == sorted(set(positions)) and all(i <= size for i in positions)
    message = pb.OrderedValues(
        coverage=[pb.Ordinal(ordinal=str(i + 1)) for i in range(size)],
        records=[pb.Ordinal(ordinal=str(i)) for i in positions],
    )
    payload = {
        "records": [{"ordinal": i} for i in positions],
        "coverage": [{"ordinal": i + 1} for i in range(size)],
        "explicit_text": None,
    }
    if accepted:
        value = OrderedValues(records=records, coverage=coverage, explicit_text=None)
        assert decode_value(encode_value(value)) == value
        assert OrderedValues.model_validate_json(json.dumps(payload)) == value
        assert decode_value(message) == value
    else:
        with pytest.raises(ValidationError):
            OrderedValues(records=records, coverage=coverage, explicit_text=None)
        with pytest.raises(ValidationError):
            OrderedValues.model_validate_json(json.dumps(payload))
        with pytest.raises(ProtoValidationError):
            decode_value(message)


@given(value=st.integers(min_value=-100, max_value=100))
@example(value=-1)
def test_cel_range_rules_run_in_constructor_and_both_wire_boundaries(value: int) -> None:
    fields = {"value": float(value), "lower": 0.0, "upper": 10.0}
    message = pb.Quantity(**fields)
    if 0 <= value <= 10:
        quantity = Quantity(**fields)
        assert decode_value(message) == quantity
        assert decode_value(encode_value(quantity)) == quantity
    else:
        with pytest.raises(ValidationError):
            Quantity(**fields)
        with pytest.raises(ProtoValidationError):
            decode_value(message)
        # Deliberately poison a frozen value to verify validation of outgoing data too.
        poisoned = Quantity(value=0.0, lower=0.0, upper=10.0)
        object.__setattr__(poisoned, "value", float(value))
        with pytest.raises(ProtoValidationError):
            encode_value(poisoned)


@given(labels=st.lists(st.text(max_size=10), max_size=10))
def test_immutable_sets_have_canonical_json_and_wire_order(labels: list[str]) -> None:
    left = OrderedValues(labels=frozenset(labels), explicit_text=None)
    right = OrderedValues(labels=frozenset(reversed(labels)), explicit_text=None)
    assert left == right
    assert left.model_dump_json() == right.model_dump_json()
    assert encode_value(left).to_binary() == encode_value(right).to_binary()
    assert decode_value(encode_value(left)) == left
    with pytest.raises(ValidationError):
        OrderedValues(labels=labels, explicit_text=None)


def test_narrowed_inheritance_and_explicit_null_preserve_presence() -> None:
    extended = ExtendedQuantity(value=1.0, lower=0.0, upper=2.0, label="extra")
    value = OrderedValues(quantity=extended, explicit_text=None)
    assert type(value.quantity) is Quantity
    assert "label" not in json.loads(value.model_dump_json())["quantity"]
    assert decode_value(encode_value(value)) == value
    with pytest.raises(ValidationError, match="explicit_text"):
        OrderedValues()
    assert Ordinal().ordinal == 1


@given(
    scalar=st.none()
    | st.booleans()
    | st.integers()
    | st.text(max_size=40)
    | st.floats(allow_nan=False, allow_infinity=False)
)
@example(scalar=2**100)
@example(scalar=True)
@example(scalar=1.0)
def test_scalar_union_domains_survive_both_json_and_wire(scalar: object) -> None:
    value = values.PureOption(key="option", value=scalar)
    restored = from_proto(to_proto(value))
    assert restored == value
    assert type(restored.value) is type(scalar)
    assert type(values.PureOption.model_validate_json(value.model_dump_json()).value) is type(
        scalar
    )


@given(status=st.sampled_from(list(core_vocabulary.ObservationStatus)))
def test_acceptance_receipt_has_only_positive_known_source_proof(
    status: core_vocabulary.ObservationStatus,
) -> None:
    source = values.RequestId(root="source")
    scope = values.Scope(owner=values.RunId(root="run"), generation=0)
    message = core_agent_facts.EvidenceAcceptanceReceipt(
        source_request=core_common.RequestId(root="source", kind="request"),
        scope=core_common.Scope(
            owner=core_common.ScopeOwner(
                owner=Oneof("run", core_common.RunId(root="run", kind="run"))
            ),
            generation="0",
        ),
        observation_sequence="0",
        status=status,
    )
    if status not in (
        core_vocabulary.ObservationStatus.UNSPECIFIED,
        core_vocabulary.ObservationStatus.UNKNOWN,
    ):
        value = values.EvidenceAcceptanceReceipt(
            source_request=source,
            scope=scope,
            observation_sequence=0,
            status=values.ObservationStatus(status.name.lower()),
        )
        assert from_proto(message) == value
    else:
        with pytest.raises(ProtoValidationError):
            from_proto(message)
        with pytest.raises(ValidationError):
            values.EvidenceAcceptanceReceipt(
                source_request=source,
                scope=scope,
                observation_sequence=0,
                status=status.name.lower(),
            )


@given(choice=st.integers(min_value=0, max_value=4))
@example(choice=2)
def test_enum_subsets_preserve_inclusions_exclusions_and_literal_enums(choice: int) -> None:
    accepted = choice in (1, 3)
    spelling = {1: "first", 2: "second", 3: "third"}.get(choice, "unknown")
    payload = {"choice": spelling, "literal": spelling}
    message = pb.EnumSelection(choice=pb.Choice(choice), literal=pb.LiteralChoice(choice))
    if accepted:
        value = EnumSelection(choice=Choice(spelling), literal=spelling)
        assert EnumSelection.model_validate_json(json.dumps(payload)) == value
        assert decode_value(message) == value
        assert decode_value(encode_value(value)) == value
    else:
        with pytest.raises(ValidationError):
            EnumSelection.model_validate_json(json.dumps(payload))
        with pytest.raises(ProtoValidationError):
            decode_value(message)
