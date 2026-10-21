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
    encode_identity,
)
from vs_proto.api import core_common, replay


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
