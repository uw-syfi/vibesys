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

from server.api.protocol import ProtocolRequest, Response, ServerMessage
from server.api.schema import (
    fully_populated_response,
    protocol_json_schema,
    require_supported_response_payload_schema,
    require_supported_run_event_schema,
    require_tagged_union_discriminants,
    response_payload_json_schema,
    response_payload_typescript,
    run_event_typescript,
)
from server.events import EventData
from vibesys.api import RunFailedData, RunFailure, ToolResultPayload

SCHEMA_PATH = Path("clients/backend-client/src/generated/protocol.schema.json")
RESPONSE_PAYLOAD_SCHEMA_PATH = Path(
    "clients/backend-client/src/generated/response-payload.schema.json"
)
RESPONSE_PAYLOAD_MODULE_PATH = Path(
    "clients/backend-client/src/generated/response-payload.schema.ts"
)
RESPONSE_PAYLOAD_FIXTURE_PATH = Path(
    "clients/backend-client/src/generated/response-payload.fixture.json"
)
RUN_EVENT_MODULE_PATH = Path("clients/backend-client/src/generated/run-event.schema.ts")

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


def test_committed_response_payload_schema_matches_python_contract() -> None:
    assert json.loads(RESPONSE_PAYLOAD_SCHEMA_PATH.read_text()) == response_payload_json_schema()


def test_committed_response_payload_module_matches_python_contract() -> None:
    assert RESPONSE_PAYLOAD_MODULE_PATH.read_text() == response_payload_typescript()


def test_committed_response_payload_fixture_is_a_real_server_model_dump() -> None:
    response = fully_populated_response()
    assert isinstance(response, Response)
    assert RESPONSE_PAYLOAD_FIXTURE_PATH.read_text() == response.model_dump_json() + "\n"


def test_committed_run_event_module_matches_python_contract() -> None:
    assert RUN_EVENT_MODULE_PATH.read_text() == run_event_typescript()


@pytest.mark.parametrize(
    "keyword",
    [
        "allOf",
        "exclusiveMinimum",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "multipleOf",
        "not",
        "oneOf",
        "pattern",
        "uniqueItems",
    ],
)
def test_response_payload_descriptor_refuses_unimplemented_validation_keywords(
    keyword: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=rf"\$: unsupported response payload schema keyword '{keyword}'",
    ):
        require_supported_response_payload_schema({"type": "string", keyword: 1})


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        ({"type": "string", "minimum": 0}, "minimum requires a numeric type"),
        ({"type": "integer", "minimum": True}, "minimum must be a finite number"),
        ({"type": "integer", "format": "date-time"}, "unsupported.*format"),
        ({"type": "integer", "const": "one"}, "const does not match its type"),
        ({"type": "object", "additionalProperties": {}}, "additionalProperties must be boolean"),
        ({"type": "object", "required": ["value", "value"]}, "unique strings"),
        ({"anyOf": []}, "anyOf must be a nonempty array"),
        ({"type": "array", "const": []}, "const must be a scalar"),
        ({"type": "object", "const": {}}, "const must be a scalar"),
        ({"const": ["value"]}, "const must be a scalar"),
        (
            {"type": "integer", "const": 1, "minimum": 2},
            "cannot combine an applicator with another shape",
        ),
    ],
)
def test_response_payload_descriptor_refuses_unenforceable_keyword_values(
    schema: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        require_supported_response_payload_schema(schema)


def test_run_event_descriptor_accepts_only_a_complete_discriminated_union() -> None:
    member = {
        "type": "object",
        "properties": {"kind": {"const": "known", "type": "string"}},
        "required": ["kind"],
    }
    descriptor = {
        "$defs": {"Known": member},
        "type": "object",
        "properties": {
            "value": {
                "oneOf": [{"$ref": "#/$defs/Known"}],
                "discriminator": {
                    "propertyName": "kind",
                    "mapping": {"known": "#/$defs/Known"},
                },
            }
        },
    }

    assert require_supported_run_event_schema(descriptor) is descriptor
    with pytest.raises(ValueError, match="mapping must name every member exactly once"):
        require_supported_run_event_schema(
            {
                **descriptor,
                "properties": {
                    "value": {
                        "oneOf": [{"$ref": "#/$defs/Known"}],
                        "discriminator": {"propertyName": "kind", "mapping": {}},
                    }
                },
            }
        )

    with pytest.raises(ValueError, match=r"mapping key 'wrong' does not match.*const 'known'"):
        require_supported_run_event_schema(
            {
                **descriptor,
                "properties": {
                    "value": {
                        "oneOf": [{"$ref": "#/$defs/Known"}],
                        "discriminator": {
                            "propertyName": "kind",
                            "mapping": {"wrong": "#/$defs/Known"},
                        },
                    }
                },
            }
        )


@pytest.mark.parametrize(
    ("choices", "mapping", "reason"),
    [
        (
            [{"$ref": "#/$defs/Known"}, {"$ref": "#/$defs/Known"}],
            {"known": "#/$defs/Known", "alias": "#/$defs/Known"},
            "members must contain unique references",
        ),
        (
            [{"$ref": "#/$defs/Known"}, {"$ref": "#/$defs/Other"}],
            {"known": "#/$defs/Known", "other": "#/$defs/Known"},
            "mapping must contain unique references",
        ),
    ],
)
def test_run_event_descriptor_rejects_repeated_tagged_union_references(
    choices: list[dict[str, str]], mapping: dict[str, str], reason: str
) -> None:
    definitions = {
        name: {
            "type": "object",
            "properties": {"kind": {"const": tag, "type": "string"}},
            "required": ["kind"],
        }
        for name, tag in (("Known", "known"), ("Other", "other"))
    }

    with pytest.raises(ValueError, match=reason):
        require_supported_run_event_schema(
            {
                "$defs": definitions,
                "type": "object",
                "properties": {
                    "value": {
                        "oneOf": choices,
                        "discriminator": {"propertyName": "kind", "mapping": mapping},
                    }
                },
            }
        )


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


def test_committed_schema_reuses_the_active_execution_list_definition() -> None:
    """Snapshots and event batches expose one shared execution checkpoint type."""
    document = _committed_schema()
    definitions = document["$defs"]

    assert definitions["RunSnapshot"]["properties"]["active_executions"] == {
        "$ref": "#/$defs/ActiveExecutions"
    }
    assert definitions["EventBatchMessage"]["properties"]["active_executions"] == {
        "$ref": "#/$defs/ActiveExecutions"
    }


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


def test_wire_run_failure_is_the_core_contract() -> None:
    """The published RunFailure schema is core's own model, so the two cannot diverge."""
    defs = protocol_json_schema()["$defs"]
    core = RunFailure.model_json_schema()
    assert defs["RunFailure"]["properties"] == core["properties"]
    assert defs["RunFailure"]["required"] == core["required"]
    assert defs["RunFailureKind"]["enum"] == core["$defs"]["RunFailureKind"]["enum"]
    assert RunFailedData.model_fields["failure"].annotation is RunFailure
