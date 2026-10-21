"""Semantic data, not helpers or policy, identifies historical work."""

from math import inf, nan
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from protobuf import Message
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
from vs_proto.api import core_common, core_leaves, replay

from .generated.vibesys.replay.testing.v1.context_pb import (
    Context,
    InvalidSetContext,
    NestedContext,
    OpaqueIdentity,
    RepeatedContext,
    RepeatedMapIdentity,
    RepeatedUnclassifiedPolicy,
    SharedContext,
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


@pytest.mark.parametrize("position", [True, -1, 1.5])
def test_position_requires_a_nonnegative_integer(position: object) -> None:
    with pytest.raises(ReplayIdentityError, match="nonnegative integer"):
        SemanticKey("implement").recorded_position(EMPTY_CAUSALITY, cast("int", position))


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


def test_semantic_keys_reject_unknown_fields() -> None:
    key = SemanticKey("operation").to_proto()
    unknown = replay.OperationKey.from_binary(key.to_binary() + b"\xa0\x06\x01")
    with pytest.raises(ReplayIdentityError, match="unknown fields"):
        SemanticKey.from_proto(unknown)


def test_unknown_fields_nested_in_identity_are_rejected() -> None:
    context = Context.from_binary(Context(candidate="a").to_binary() + b"\xa0\x06\x01")
    with pytest.raises(ReplayIdentityError, match="unknown fields"):
        encode_identity(NestedContext(context=context), contract="plan.v1")


def test_set_annotation_on_a_scalar_fails_at_the_writer() -> None:
    with pytest.raises(ReplayIdentityError, match="requires a repeated field"):
        encode_identity(InvalidSetContext(evidence="a"), contract="plan.v1")


def test_writer_encoding_v1_is_pinned() -> None:
    expected = bytes.fromhex(
        "766962657379732e6964656e7469747901000000000000000c63616e6469646174652e76310000000000000018766962657379732e636f72652e76312e4964656e7469747999fc031b5f55a1e0ad2c275528eb68c27195ed88613cddca3532f96da8df273a00000000000000030a0178"
    )
    value = encode_identity(core_common.Identity(root="x"), contract="candidate.v1")
    assert value.version == 1
    assert value.canonical == expected


@pytest.mark.parametrize("message", [OpaqueIdentity(), RepeatedMapIdentity()])
def test_forbidden_schemas_nested_in_empty_repeated_fields_are_rejected(message: Message) -> None:
    with pytest.raises(ReplayIdentityError, match=r"Any|map"):
        encode_identity(message, contract="plan.v1")


def test_repeated_context_fields_are_classified_even_when_absent() -> None:
    with pytest.raises(ReplayIdentityError, match="unclassified prompt field"):
        classify_context(RepeatedUnclassifiedPolicy(), contract="plan.v1")


def test_policy_inside_repeated_identity_context_does_not_change_identity() -> None:
    before = RepeatedContext(
        contexts=[Context(candidate="a", wording="old"), Context(candidate="b", wording="old")]
    )
    after = RepeatedContext(
        contexts=[Context(candidate="a", wording="new"), Context(candidate="b", wording="new")]
    )
    identity = classify_context(before, contract="plan.v1").identity
    assert identity == classify_context(after, contract="plan.v1").identity
    after.contexts.reverse()
    assert identity != classify_context(after, contract="plan.v1").identity


def test_typed_context_inherits_classification_for_shared_facts() -> None:
    before = SharedContext(
        candidate=core_leaves.RevisionRef(
            revision_id=core_common.RevisionId(root="candidate-a", kind="revision"),
            digest="git-commit:candidate-a",
        ),
        allowance=replay.ReplayAllowance(remaining=1),
    )
    after = SharedContext(
        candidate=core_leaves.RevisionRef(
            revision_id=core_common.RevisionId(root="candidate-a", kind="revision"),
            digest="git-commit:candidate-a",
        ),
        allowance=replay.ReplayAllowance(remaining=99),
    )
    identity = classify_context(before, contract="plan.v1").identity
    assert identity == classify_context(after, contract="plan.v1").identity
    assert after.candidate is not None
    after.candidate.digest = "git-commit:candidate-b"
    assert identity != classify_context(after, contract="plan.v1").identity


def test_a_shared_value_alone_is_not_a_declared_prompt_context() -> None:
    with pytest.raises(ReplayIdentityError, match="must declare replay_context"):
        classify_context(core_common.Identity(root="candidate-a"), contract="plan.v1")
