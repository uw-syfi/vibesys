"""Schema identity is stable under comments/order and sensitive to contracts."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from protobuf.wkt.google.protobuf.descriptor_pb import (
    DescriptorProto,
    FileDescriptorProto,
    FileDescriptorSet,
    SourceCodeInfo,
)
from scripts.generate_proto_hashes import schema_hash


@given(order=st.permutations(["a.proto", "b.proto"]))
def test_schema_hash_ignores_file_order_other_packages_and_source_comments(
    order: list[str],
) -> None:
    files = [FileDescriptorProto(name=name, package="vibesys.api.v1") for name in order]
    expected = schema_hash(FileDescriptorSet(file=files).to_binary(), "vibesys.api.v1")
    files[0].source_code_info = SourceCodeInfo(
        location=[SourceCodeInfo.Location(leading_comments="New documentation")],
    )
    files.append(FileDescriptorProto(name="other.proto", package="vibesys.store.v1"))
    assert schema_hash(FileDescriptorSet(file=files).to_binary(), "vibesys.api.v1") == expected


def test_schema_hash_changes_when_a_message_is_added() -> None:
    file = FileDescriptorProto(name="a.proto", package="vibesys.api.v1")
    before = schema_hash(FileDescriptorSet(file=[file]).to_binary(), "vibesys.api.v1")
    file.message_type.append(DescriptorProto(name="NewMessage"))
    assert schema_hash(FileDescriptorSet(file=[file]).to_binary(), "vibesys.api.v1") != before


def test_schema_hash_rejects_a_missing_package() -> None:
    with pytest.raises(ValueError, match=r"vibesys\.api\.v1"):
        schema_hash(FileDescriptorSet().to_binary(), "vibesys.api.v1")
