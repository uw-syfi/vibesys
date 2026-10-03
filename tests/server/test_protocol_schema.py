"""Generated backend-client schema parity and tagged-union discriminants."""

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from server.api.protocol import ProtocolRequest, ServerMessage
from server.api.schema import protocol_json_schema, require_tagged_union_discriminants
from server.events import EventData
from vibesys.api import ToolResultPayload

SCHEMA_PATH = Path("clients/backend-client/src/generated/protocol.schema.json")

# The public alias of every tagged union the protocol publishes. A member model
# is only reachable through one of these, which is why the exported schema may
# call the discriminant required.
TAGGED_UNION_ALIASES = (ProtocolRequest, ServerMessage, EventData, ToolResultPayload)

IDENTIFIERS = st.text(alphabet="abcdefg", min_size=1, max_size=3)


def _committed_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def _discriminated_members(node: object, path: str) -> Iterator[tuple[str, str, str]]:
    """Yield ``(path, discriminant, member ref)`` for every tagged union member.

    A deliberately independent walk of the published document: the invariant is
    about the artifact clients consume, not about the exporter's internals.
    """
    if isinstance(node, dict):
        discriminator = node.get("discriminator")
        if isinstance(discriminator, dict) and isinstance(node.get("oneOf"), list):
            for tag, ref in discriminator["mapping"].items():
                yield f"{path}/{tag}", discriminator["propertyName"], ref
        for key, value in node.items():
            yield from _discriminated_members(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _discriminated_members(value, f"{path}/{index}")


def _definition(document: dict[str, Any], ref: str) -> dict[str, Any]:
    return document["$defs"][ref.removeprefix("#/$defs/")]


def test_committed_protocol_schema_matches_python_contract() -> None:
    assert _committed_schema() == protocol_json_schema()


def test_committed_schema_requires_every_tagged_union_discriminant() -> None:
    """Regression for #860: an optional tag is a union no client can narrow."""
    document = _committed_schema()
    members = list(_discriminated_members(document, ""))
    assert members, "expected the protocol document to publish tagged unions"
    optional = [
        f"{path} -> {ref}.{discriminant}"
        for path, discriminant, ref in members
        if discriminant not in _definition(document, ref).get("required", [])
    ]
    assert optional == []


def test_committed_schema_discriminants_are_literal_tags() -> None:
    document = _committed_schema()
    non_literal = [
        f"{path} -> {ref}.{discriminant}"
        for path, discriminant, ref in _discriminated_members(document, "")
        if "const" not in _definition(document, ref)["properties"][discriminant]
    ]
    assert non_literal == []


@pytest.mark.parametrize("alias", TAGGED_UNION_ALIASES)
def test_validator_rejects_a_member_without_its_discriminant(alias: object) -> None:
    """Why the schema may require the tag: the union never accepts it missing."""
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(alias).validate_python({})
    assert [error["type"] for error in caught.value.errors()] == ["union_tag_not_found"]


@st.composite
def _one_union_documents(draw: st.DrawFn) -> tuple[dict[str, Any], str, list[str]]:
    """Build a schema document holding one tagged union, its tag, and members."""
    tag = draw(st.sampled_from(["kind", "type"]))
    names = draw(st.lists(IDENTIFIERS, min_size=1, max_size=4, unique=True))
    definitions: dict[str, Any] = {}
    for index, name in enumerate(names):
        drawn = draw(st.lists(IDENTIFIERS, max_size=3, unique=True))
        properties: dict[str, Any] = {tag: {"const": f"tag{index}"}}
        properties.update({key: {"type": "string"} for key in drawn if key != tag})
        definitions[name] = {
            "type": "object",
            "properties": properties,
            "required": draw(st.lists(st.sampled_from(sorted(properties)), unique=True)),
        }
    document = {
        "$defs": definitions,
        "properties": {
            "message": {
                "oneOf": [{"$ref": f"#/$defs/{name}"} for name in names],
                "discriminator": {
                    "propertyName": tag,
                    "mapping": {f"tag{i}": f"#/$defs/{n}" for i, n in enumerate(names)},
                },
            },
        },
    }
    return document, tag, names


@given(_one_union_documents())
def test_transform_requires_the_tag_and_preserves_the_rest(
    case: tuple[dict[str, Any], str, list[str]],
) -> None:
    document, tag, names = case
    original = copy.deepcopy(document)
    result = require_tagged_union_discriminants(document)
    assert document == original, "the transform must not modify its argument"
    assert require_tagged_union_discriminants(result) == result, "must be idempotent"
    assert {key: value for key, value in result.items() if key != "$defs"} == {
        key: value for key, value in original.items() if key != "$defs"
    }
    for name in names:
        member, source = result["$defs"][name], original["$defs"][name]
        expected = {*source["required"], tag}
        assert member["required"] == [key for key in source["properties"] if key in expected]
        assert {key: value for key, value in member.items() if key != "required"} == {
            key: value for key, value in source.items() if key != "required"
        }


def test_union_without_a_mapping_names_its_path() -> None:
    document = {
        "properties": {
            "message": {
                "oneOf": [{"$ref": "#/$defs/A"}],
                "discriminator": {"propertyName": "kind"},
            },
        },
    }
    with pytest.raises(ValueError, match=r"^/properties/message: tagged union needs"):
        require_tagged_union_discriminants(document)


def test_unresolvable_member_reference_names_its_path() -> None:
    document = {
        "properties": {
            "message": {
                "oneOf": [{"$ref": "#/$defs/Missing"}],
                "discriminator": {"propertyName": "kind", "mapping": {"a": "#/$defs/Missing"}},
            },
        },
    }
    with pytest.raises(ValueError, match=r"mapping/a: schema reference .* does not resolve"):
        require_tagged_union_discriminants(document)


def test_member_missing_the_discriminant_property_names_its_path() -> None:
    document = {
        "$defs": {"A": {"type": "object", "properties": {"other": {"type": "string"}}}},
        "properties": {
            "message": {
                "oneOf": [{"$ref": "#/$defs/A"}],
                "discriminator": {"propertyName": "kind", "mapping": {"a": "#/$defs/A"}},
            },
        },
    }
    with pytest.raises(ValueError, match=r"mapping/a: union member declares no 'kind'"):
        require_tagged_union_discriminants(document)
