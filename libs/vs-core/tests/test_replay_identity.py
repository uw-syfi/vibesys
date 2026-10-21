"""Semantic data, not helpers or policy, identifies historical work."""

from math import inf, nan

import pytest
from hypothesis import given
from hypothesis import strategies as st
from protobuf.wkt.google.protobuf.any_pb import Any
from protobuf.wkt.google.protobuf.struct_pb import Struct

from vs_core.api.replay import (
    EMPTY_CAUSALITY,
    ReplayIdentityError,
    SemanticKey,
    chain_dependency,
    classify_context,
    encode_identity,
)
from vs_proto.api import core_common, replay

from .generated.vibesys.replay.testing.v1.context_pb import (
    Context,
    NestedContext,
    UnclassifiedPolicy,
)


@given(value=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=100))
def test_encoding_is_stable_and_retains_bytes(value: str) -> None:
    message = core_common.Identity(root=value)
    encoded = encode_identity(message, contract="candidate.v1")
    assert encoded == encode_identity(
        core_common.Identity.from_binary(message.to_binary()), contract="candidate.v1"
    )
    assert encoded.canonical != encode_identity(message, contract="candidate.v2").canonical
    changed = chain_dependency(EMPTY_CAUSALITY, encoded.digest)
    assert encoded != encode_identity(message, contract="candidate.v1", dependencies=changed)
    assert len(changed) == len(EMPTY_CAUSALITY)


def test_unknown_identity_fields_are_rejected() -> None:
    value = core_common.Identity.from_binary(b"\x0a\x01x\xa0\x06\x01")
    with pytest.raises(ReplayIdentityError, match="unknown fields"):
        encode_identity(value, contract="input.v1")


@pytest.mark.parametrize("message", [Any(), Struct()])
def test_opaque_identity_schemas_are_rejected_even_when_empty(message: Any | Struct) -> None:
    with pytest.raises(ReplayIdentityError, match=r"Any|map"):
        encode_identity(message, contract="input.v1")


@pytest.mark.parametrize("value", [inf, -inf, nan])
def test_nonfinite_identity_is_rejected(value: float) -> None:
    with pytest.raises(ReplayIdentityError, match="nonfinite"):
        encode_identity(replay.ReplayAllowance(remaining=value), contract="input.v1")


def test_signed_zero_has_one_identity_encoding() -> None:
    assert encode_identity(
        replay.ReplayAllowance(remaining=-0.0), contract="input.v1"
    ) == encode_identity(replay.ReplayAllowance(remaining=0.0), contract="input.v1")


@given(position=st.integers(min_value=0, max_value=10000))
def test_key_scope_is_typed_and_roundtrips(position: int) -> None:
    key = (
        SemanticKey("implement")
        .recorded_position(EMPTY_CAUSALITY, position)
        .sample("a")
        .revision("r")
    )
    assert SemanticKey.from_proto(key.to_proto()) == key
    assert key != SemanticKey("implement").item(str(position))


def test_position_without_recorded_parent_is_invalid() -> None:
    with pytest.raises(ReplayIdentityError, match="recorded outcome"):
        SemanticKey("implement").recorded_position(b"not-a-digest", 1)


def test_prompt_context_requires_classification_even_for_absent_policy_fields() -> None:

    with pytest.raises(ReplayIdentityError, match="unclassified prompt field"):
        classify_context(UnclassifiedPolicy(), contract="plan.v1")


@given(wording=st.text(), evidence=st.lists(st.sampled_from(["a", "b", "c"])))
def test_typed_context_centralizes_sets_and_keeps_ordered_fields(
    wording: str, evidence: list[str]
) -> None:

    original = Context(candidate="a", wording="old", evidence=evidence, ordered=["a", "b"])
    changed = Context(
        candidate="a", wording=wording, evidence=list(reversed(evidence)), ordered=["a", "b"]
    )
    before = classify_context(original, contract="plan.v1")
    after = classify_context(changed, contract="plan.v1")
    assert before.identity == after.identity
    assert after.message() == Context(
        candidate="a", wording=wording, evidence=sorted(set(evidence)), ordered=["a", "b"]
    )
    changed.candidate = "b"
    assert before.identity != classify_context(changed, contract="plan.v1").identity
    changed.candidate = "a"
    changed.ordered.reverse()
    assert before.identity != classify_context(changed, contract="plan.v1").identity
    nested = classify_context(NestedContext(context=original), contract="plan.v1")
    original.wording = "new"
    assert (
        nested.identity
        == classify_context(NestedContext(context=original), contract="plan.v1").identity
    )
