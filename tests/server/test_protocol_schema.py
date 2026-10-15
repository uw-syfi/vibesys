"""Generated contracts schema parity and tagged-union discriminants."""

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from server.api.protocol import (
    MAX_ACTIVE_EXECUTIONS_CHUNKS,
    ProtocolRequest,
    Response,
    ServerMessage,
)
from server.api.schema import (
    fully_populated_response,
    host_helper_json_schema,
    host_helper_typescript,
    protocol_json_schema,
    require_supported_response_payload_schema,
    require_supported_run_event_schema,
    require_tagged_union_discriminants,
    response_payload_json_schema,
    response_payload_typescript,
    run_event_typescript,
)
from server.events import EventData
from server.host_api import HelperFeature
from vibesys.api import RunFailedData, RunFailure, ToolResultPayload

SCHEMA_PATH = Path("clients/libs/contracts/src/generated/protocol.schema.json")
HOST_HELPER_SCHEMA_PATH = Path("clients/libs/contracts/src/generated/host-helper.schema.json")
RESPONSE_PAYLOAD_SCHEMA_PATH = Path(
    "clients/libs/contracts/src/generated/response-payload.schema.json"
)
RESPONSE_PAYLOAD_MODULE_PATH = Path(
    "clients/libs/contracts/src/generated/response-payload.schema.ts"
)
RESPONSE_PAYLOAD_FIXTURE_PATH = Path(
    "clients/libs/contracts/src/generated/response-payload.fixture.json"
)
RUN_EVENT_MODULE_PATH = Path("clients/libs/contracts/src/generated/run-event.schema.ts")
HOST_HELPER_MODULE_PATH = Path("clients/libs/contracts/src/generated/host-helper.schema.ts")

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


def test_committed_host_helper_schema_matches_python_contract() -> None:
    assert json.loads(HOST_HELPER_SCHEMA_PATH.read_text()) == host_helper_json_schema()


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


def test_committed_host_helper_module_matches_python_contract() -> None:
    assert HOST_HELPER_MODULE_PATH.read_text() == host_helper_typescript()


def test_host_helper_schema_exports_the_instance_log_utf8_byte_limit() -> None:
    document = host_helper_json_schema()
    text = document["$defs"]["AvailableLogTail"]["properties"]["text"]

    assert text["x-vibesys-max-utf8-bytes"] == 64 * 1024


def test_host_helper_schema_exports_the_authoritative_request_feature_gates() -> None:
    document = host_helper_json_schema()
    gates = document["x-vibesys-feature-by-request"]

    assert gates == {
        "instances.inspect": "instances_inspect",
        "instances.watch": "instances_watch",
        "instances.unwatch": "instances_watch",
        "paths.complete": "paths_complete",
    }
    request_tags = document["properties"]["request"]["discriminator"]["mapping"]
    assert gates.keys() <= request_tags.keys()
    assert set(gates.values()) == {feature.value for feature in HelperFeature}


@pytest.mark.parametrize(
    "keyword",
    [
        "allOf",
        "exclusiveMinimum",
        "maxItems",
        "maximum",
        "minItems",
        "multipleOf",
        "not",
        "oneOf",
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
        ({"type": "integer", "minLength": 1}, "string constraints require type 'string'"),
        ({"type": "string", "minLength": True}, "minLength must be a nonnegative integer"),
        ({"type": "string", "maxLength": -1}, "maxLength must be a nonnegative integer"),
        ({"type": "string", "minLength": 2, "maxLength": 1}, "cannot exceed maxLength"),
        ({"type": "string", "pattern": 1}, "pattern must be a string"),
        ({"type": "string", "pattern": "["}, "pattern must be a valid regular expression"),
        (
            {"type": "string", "pattern": r"(?P<name>a)"},
            "pattern must use the portable client regular-expression dialect",
        ),
        (
            {"type": "string", "pattern": r"(?>a)"},
            "pattern must use the portable client regular-expression dialect",
        ),
        (
            {"type": "string", "pattern": "]"},
            "pattern must use the portable client regular-expression dialect",
        ),
        (
            {"type": "string", "pattern": "}"},
            "pattern must use the portable client regular-expression dialect",
        ),
        (
            {"type": "string", "pattern": "a}"},
            "pattern must use the portable client regular-expression dialect",
        ),
        (
            {"type": "string", "pattern": "{"},
            "pattern must use the portable client regular-expression dialect",
        ),
        (
            {"type": "string", "pattern": "a{"},
            "pattern must use the portable client regular-expression dialect",
        ),
        *(
            (
                {"type": "string", "pattern": pattern},
                "pattern must use the portable client regular-expression dialect",
            )
            for pattern in (r"(?=a)*", r"(?!a)+", r"(?=a){1}", r"(?=a)??")
        ),
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


def test_response_payload_descriptor_accepts_enforced_string_constraints() -> None:
    descriptor = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^[a-z0-9._-]+$",
    }

    assert require_supported_response_payload_schema(descriptor) is descriptor


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
        "anyOf": [
            {"$ref": "#/$defs/ActiveExecutions"},
            {"type": "null"},
        ]
    }
    assert definitions["EventBatchMessage"]["properties"]["active_executions_checkpoint_id"] == {
        "anyOf": [
            {"pattern": "^[0-9a-f]{64}$", "type": "string"},
            {"type": "null"},
        ],
        "default": None,
        "title": "Active Executions Checkpoint Id",
    }
    ordinary, deferred = definitions["EventBatchMessage"]["oneOf"]
    assert ordinary["properties"]["active_executions"] == {"$ref": "#/$defs/ActiveExecutions"}
    assert ordinary["properties"]["active_executions_checkpoint_id"] == {
        "default": None,
        "title": "Active Executions Checkpoint Id",
        "type": "null",
    }
    assert deferred["properties"]["active_executions"] == {"type": "null"}
    assert deferred["properties"]["active_executions_checkpoint_id"] == {
        "pattern": "^[0-9a-f]{64}$",
        "type": "string",
    }
    assert set(deferred["required"]) >= {
        "type",
        "events",
        "active_executions",
        "active_executions_checkpoint_id",
    }

    chunk = definitions["ActiveExecutionsChunkMessage"]
    assert chunk["properties"]["payload"]["maxLength"] == 64 * 1024
    assert chunk["properties"]["chunk_count"]["maximum"] == MAX_ACTIVE_EXECUTIONS_CHUNKS
    assert set(chunk["required"]) >= {
        "checkpoint_id",
        "chunk_index",
        "chunk_count",
        "payload",
        "through_sequence",
        "store_id",
        "history_after_sequence",
    }


@pytest.mark.parametrize(
    ("checkpoint_fields", "outcome"),
    [
        ({}, "accepted"),
        ({"active_executions": []}, "accepted"),
        ({"active_executions_checkpoint_id": None}, "accepted"),
        (
            {
                "active_executions": None,
                "active_executions_checkpoint_id": "a" * 64,
            },
            "accepted",
        ),
        ({"active_executions": None}, "rejected"),
        (
            {
                "active_executions": None,
                "active_executions_checkpoint_id": None,
            },
            "rejected",
        ),
        (
            {
                "active_executions": [],
                "active_executions_checkpoint_id": "a" * 64,
            },
            "rejected",
        ),
        ({"active_executions_checkpoint_id": "a" * 64}, "rejected"),
    ],
)
def test_draft_2020_schema_enforces_deferred_checkpoint_relationship(
    checkpoint_fields: dict[str, object], outcome: str
) -> None:
    document = _committed_schema()
    Draft202012Validator.check_schema(document)
    validator = Draft202012Validator(
        {"$ref": "#/$defs/EventBatchMessage", "$defs": document["$defs"]}
    )
    errors = list(validator.iter_errors({"type": "event_batch", "events": [], **checkpoint_fields}))
    assert ("accepted" if errors == [] else "rejected") == outcome


@pytest.mark.parametrize(
    "checkpoint_fields",
    [
        {"active_executions": None},
        {
            "active_executions": None,
            "active_executions_checkpoint_id": None,
        },
        {
            "active_executions": [],
            "active_executions_checkpoint_id": "a" * 64,
        },
        {"active_executions_checkpoint_id": "a" * 64},
    ],
)
def test_server_message_rejects_inconsistent_deferred_checkpoint_fields(
    checkpoint_fields: dict[str, object],
) -> None:
    """The Python source contract owns the relation enforced by every client."""
    with pytest.raises(
        ValidationError,
        match=("active_executions_checkpoint_id is present exactly when active_executions is null"),
    ):
        TypeAdapter(ServerMessage).validate_python(
            {"type": "event_batch", "events": [], **checkpoint_fields}
        )


@pytest.mark.parametrize(
    "checkpoint_fields",
    [
        {},
        {"active_executions": []},
        {"active_executions_checkpoint_id": None},
        {
            "active_executions": None,
            "active_executions_checkpoint_id": "a" * 64,
        },
    ],
)
def test_server_message_accepts_consistent_deferred_checkpoint_fields(
    checkpoint_fields: dict[str, object],
) -> None:
    message = TypeAdapter(ServerMessage).validate_python(
        {"type": "event_batch", "events": [], **checkpoint_fields}
    )
    assert message.type == "event_batch"


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


def test_thread_creation_request_no_longer_accepts_a_driver() -> None:
    """The wire request names provider and model only; ``driver`` is an unknown key."""
    request = TypeAdapter(ProtocolRequest)
    request.validate_python(
        {
            "type": "query.chat_thread_create",
            "timestamp": "2026-01-01T00:00:00Z",
            "provider": "codex",
        }
    )
    with pytest.raises(ValidationError, match="driver"):
        request.validate_python(
            {
                "type": "query.chat_thread_create",
                "timestamp": "2026-01-01T00:00:00Z",
                "driver": "agentshim",
            }
        )
