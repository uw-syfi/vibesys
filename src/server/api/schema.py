"""Export the wire contracts as JSON Schema for generated clients.

Two contracts are published: the run protocol (``protocol.schema.json``) and the host
helper's messages (``host-helper.schema.json``, from ``server.host_api``).
"""

from __future__ import annotations

import copy
import json
import math
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never

from pydantic import BaseModel, ConfigDict, create_model

from server.api.iterations import IterationSlice
from server.api.protocol import ProtocolRequest, Response, RunSnapshot, ServerMessage
from server.events import RunEvent
from server.host_api import (
    HELPER_FIELD_FEATURES,
    HELPER_REQUEST_FEATURES,
    ChannelEnd,
    HelperMessage,
    HelperRequest,
    RunStartCompensationFailure,
)
from server.host_run_events import EVENT_TYPES_BY_KINDS
from vibesys.api import (
    IterationKind,
    IterationMetric,
    IterationStatus,
    IterationSummary,
    LogOwner,
)
from vibesys.api.display_catalog import code_ids, display_names
from vs_logs.api import NarrationLine

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

_LOCAL_REF_PREFIX = "#/"
_OPTION_ARGUMENT_COUNT = 2
_RESPONSE_FIELDS_VALIDATED_BY_PARSER = frozenset(
    {
        "protocol_version",
        "request_id",
        "client_id",
        "timestamp",
        "ok",
        "error",
        "diagnostic",
        "events",
    }
)
_RESPONSE_DESCRIPTOR_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "const",
        "default",
        "description",
        "enum",
        "format",
        "items",
        "maxLength",
        "minimum",
        "minLength",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
    }
)
_RUN_EVENT_DESCRIPTOR_KEYWORDS = _RESPONSE_DESCRIPTOR_KEYWORDS | {
    "discriminator",
    "oneOf",
}
_RESPONSE_DESCRIPTOR_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
_CLIENT_PATTERN_GROUP_PREFIXES = frozenset({"?:", "?=", "?!"})
_CLIENT_PATTERN_SIMPLE_ESCAPES = frozenset(r"\.^$*+?{}[]()|/sSdDwWbBfnrtv")


class UnsupportedResponsePayloadSchemaError(ValueError):
    """A generated response descriptor uses semantics the client cannot enforce."""

    def __init__(self, path: str, reason: str) -> None:
        """Describe the unsupported schema construct at its document path."""
        super().__init__(f"{path}: {reason}")


class MalformedResponsePayloadSchemaError(TypeError):
    """A generated response descriptor is structurally invalid."""

    def __init__(self, path: str) -> None:
        """Describe the invalid schema node at its document path."""
        super().__init__(f"{path}: response payload schema node must be an object")


class ProtocolDocument(BaseModel):
    """Root schema document for the public server protocol."""

    model_config = ConfigDict(extra="forbid")

    request: ProtocolRequest
    response: Response
    event: RunEvent
    snapshot: RunSnapshot
    server_message: ServerMessage
    log_owner: LogOwner
    """What a run's log stream is for; the host helper's log listing carries it."""
    narration_line: NarrationLine
    """One JSON line of a ``run_narration`` stream, which a log view decodes."""


class HostHelperDocument(BaseModel):
    """Root schema document for the host helper: what a client sends and what it receives."""

    model_config = ConfigDict(extra="forbid")

    request: HelperRequest
    message: HelperMessage
    channel_end: ChannelEnd
    compensation_failure: RunStartCompensationFailure


def host_helper_json_schema() -> dict[str, Any]:
    """Return the published JSON Schema document for the host helper's messages.

    The committed ``host-helper.schema.json`` and its generated TypeScript types come
    from here; another helper or client implementation generates its types from it.
    """
    document = require_tagged_union_discriminants(HostHelperDocument.model_json_schema())
    document["x-vibesys-feature-by-request"] = _host_helper_feature_by_request(document)
    document["x-vibesys-feature-by-field"] = _host_helper_feature_by_field(document)
    document["x-vibesys-event-types-by-kinds"] = {
        kinds.value: list(types) for kinds, types in EVENT_TYPES_BY_KINDS.items()
    }
    return document


def _host_helper_feature_by_request(document: dict[str, Any]) -> dict[str, str]:
    """Export the server's request gates, refusing a class outside the request union."""
    request = document["properties"]["request"]
    request_tags = request["discriminator"]["mapping"]
    result: dict[str, str] = {}
    for request_type, feature in HELPER_REQUEST_FEATURES.items():
        tag = request_type.model_fields["type"].default
        if not isinstance(tag, str) or tag not in request_tags:
            message = f"gated helper request {request_type.__name__} has no request-union tag"
            raise ValueError(message)
        if tag in result:
            message = f"helper request tag {tag!r} has more than one feature gate"
            raise ValueError(message)
        result[tag] = feature.value
    return result


def _host_helper_feature_by_field(document: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Export the server's optional-field gates, refusing a field the request does not have."""
    request_tags = document["properties"]["request"]["discriminator"]["mapping"]
    result: dict[str, dict[str, str]] = {}
    for request_type, fields in HELPER_FIELD_FEATURES.items():
        tag = request_type.model_fields["type"].default
        if not isinstance(tag, str) or tag not in request_tags:
            message = f"gated helper request {request_type.__name__} has no request-union tag"
            raise ValueError(message)
        for name in fields:
            if name not in request_type.model_fields:
                message = f"gated field {name!r} is not a field of {request_type.__name__}"
                raise ValueError(message)
        result[tag] = {name: feature.value for name, feature in fields.items()}
    return result


_HOST_HELPER_DESCRIPTOR_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "const",
        "default",
        "description",
        "discriminator",
        "enum",
        "exclusiveMinimum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "oneOf",
        "pattern",
        "prefixItems",
        "properties",
        "required",
        "title",
        "type",
        # Consumed by @vibesys/contracts/host-helper as the semantic log-tail byte limit.
        "x-vibesys-max-utf8-bytes",
        # Generated from HELPER_REQUEST_FEATURES; both helper implementations consume it.
        "x-vibesys-feature-by-request",
        # Generated from HELPER_FIELD_FEATURES; both helper implementations consume it.
        "x-vibesys-feature-by-field",
        # Generated from EVENT_TYPES_BY_KINDS: the event types a `runs.read_events` kind selects.
        "x-vibesys-event-types-by-kinds",
    }
)
"""What the TypeScript walker's strict mode enforces (``contracts/src/schema-walker.ts``)."""


def host_helper_typescript() -> str:
    """Return the generated module the desktop validates every helper message with.

    The helper's contract is strict (``extra="forbid"``, versions negotiated by
    ``hello``), so the client walks this descriptor strictly. A keyword the walker
    does not enforce fails generation instead of passing unchecked.
    """
    document = host_helper_json_schema()
    _require_keywords(document, _HOST_HELPER_DESCRIPTOR_KEYWORDS, "$")
    return _schema_typescript("hostHelperSchema", document)


def _require_keywords(node: object, supported: frozenset[str], path: str) -> None:
    """Refuse a schema keyword outside ``supported`` anywhere in ``node``, naming its path."""
    if isinstance(node, list):
        for index, child in enumerate(node):
            _require_keywords(child, supported, f"{path}[{index}]")
        return
    if not isinstance(node, dict):
        return
    for key, child in node.items():
        if key not in supported:
            raise UnsupportedResponsePayloadSchemaError(path, f"unsupported schema keyword {key!r}")
        if key in {"$defs", "properties"} and isinstance(child, dict):
            for name, member in child.items():
                _require_keywords(member, supported, f"{path}.{name}")
        elif key in {"items", "anyOf", "oneOf", "prefixItems"}:
            _require_keywords(child, supported, f"{path}.{key}")


type SpanAttributeModels = Mapping[str, Mapping[str, type[BaseModel]]]
"""For each orchestration, the attribute model of each kind of span it labels."""


def protocol_json_schema(span_attributes: SpanAttributeModels | None = None) -> dict[str, Any]:
    """Return the published JSON Schema document for the server protocol.

    This is the authoritative export. The committed
    ``protocol.schema.json`` artifact and every generated client come from
    here, not from ``ProtocolDocument.model_json_schema()`` directly.
    ``span_attributes`` are the orchestrations' span kinds (the composition root supplies the
    built-in ones): the document's ``span_attributes`` holds each kind's attribute schema, so a
    span label's ``attrs_json`` has a generated type per kind.
    """
    document = create_model(
        "ProtocolDocument",
        __base__=ProtocolDocument,
        span_attributes=(_span_attributes_model(span_attributes or {}), None),
    )
    return require_tagged_union_discriminants(document.model_json_schema())


def _span_attributes_model(span_attributes: SpanAttributeModels) -> type[BaseModel]:
    """One optional field per orchestration, each one optional field per kind of span."""
    fields: dict[str, Any] = {}
    for orchestration, kinds in sorted(span_attributes.items()):
        name = "".join(part.title() for part in orchestration.replace("-", "_").split("_"))
        per_kind: dict[str, Any] = {kind: (m | None, None) for kind, m in sorted(kinds.items())}
        model = create_model(f"{name}SpanAttributes", __config__=_STRICT, **per_kind)
        fields[orchestration] = (model | None, None)
    return create_model("SpanAttributes", __config__=_STRICT, **fields)


_STRICT = ConfigDict(extra="forbid")


def response_payload_json_schema() -> dict[str, Any]:
    """Return the compact generated descriptor for response-only client validation.

    The client parser owns the response envelope, diagnostics, and event-list
    checks already, so this descriptor retains every other ``Response`` field.
    Adding a response payload therefore changes this artifact automatically,
    without shipping request and stream schemas to browser clients.
    """
    document = protocol_json_schema()
    response = document["$defs"]["Response"]
    properties = response["properties"]
    payloads = {
        key: value
        for key, value in properties.items()
        if key not in _RESPONSE_FIELDS_VALIDATED_BY_PARSER
    }
    references = _referenced_definitions(document["$defs"], payloads)
    descriptor = {
        "$defs": {key: value for key, value in document["$defs"].items() if key in references},
        "type": "object",
        "properties": payloads,
    }
    return require_supported_response_payload_schema(descriptor)


def response_payload_typescript() -> str:
    """Return a generated module that works across the declared Node 20 range.

    JSON import attributes only reached Node 20 partway through that release
    line. Embedding the descriptor in an ordinary TypeScript module keeps the
    browser and Node entry points on one artifact without narrowing the
    transport packages' existing runtime contract.
    """
    return _schema_typescript("responsePayloadSchema", response_payload_json_schema())


def run_event_json_schema() -> dict[str, Any]:
    """Return the compact generated descriptor for one ``RunEvent``.

    The event model is copied as the root and only definitions reachable from
    it are retained. Tagged unions stay discriminated so the TypeScript walker
    can validate a known member deeply while deliberately accepting a newer
    member whose tag this client has not generated yet.
    """
    document = protocol_json_schema()
    definitions = document["$defs"]
    event = copy.deepcopy(definitions["RunEvent"])
    references = _referenced_definitions(definitions, event)
    descriptor = {
        "$defs": {key: value for key, value in definitions.items() if key in references},
        **event,
    }
    return require_supported_run_event_schema(descriptor)


def run_event_typescript() -> str:
    """Return the generated browser-compatible event-validation module."""
    return _schema_typescript("runEventSchema", run_event_json_schema())


def display_names_typescript() -> str:
    """Return the generated module of the role names a person reads and the ids a screen must not draw.

    One definition (``vibesys.api.display_catalog``) with the TypeScript clients' copy
    generated from it, so a screen never maps an id itself and the two cannot drift.
    """
    names = json.dumps(display_names().model_dump(), indent=2)
    ids = json.dumps(code_ids().model_dump(), indent=2)
    return (
        "/* Generated from the Python display names (vibesys.api.display_catalog). Do not edit. */\n\n"
        f"export const DISPLAY_NAMES = {names} as const;\n\n"
        f"export const CODE_IDS = {ids} as const;\n"
    )


def _schema_typescript(name: str, schema: dict[str, Any]) -> str:
    """Embed one generated descriptor without narrowing the Node 20 range."""
    document = json.dumps(schema, indent=2)
    return (
        "/* Generated from the Python protocol models. Do not edit. */\n\n"
        f"const {name}: Record<string, unknown> = {document};\n\n"
        f"export default {name};\n"
    )


def require_supported_response_payload_schema(document: dict[str, Any]) -> dict[str, Any]:
    """Refuse descriptor keywords the TypeScript response walker cannot enforce.

    The descriptor is executable client validation, not documentation. Unknown
    response object fields and string enum members deliberately remain open for
    forward compatibility, but any newly emitted schema constraint must be
    implemented by the walker before generation can succeed.
    """
    return _require_supported_client_schema(document, allow_tagged_unions=False)


def require_supported_run_event_schema(document: dict[str, Any]) -> dict[str, Any]:
    """Refuse event-schema constructs the TypeScript walker cannot enforce."""
    return _require_supported_client_schema(document, allow_tagged_unions=True)


def _require_supported_client_schema(
    document: dict[str, Any], *, allow_tagged_unions: bool
) -> dict[str, Any]:
    """Validate one executable descriptor against the TypeScript walker's grammar."""
    definitions = document.get("$defs", {})
    if not isinstance(definitions, dict):
        raise UnsupportedResponsePayloadSchemaError(
            "$", "response payload schema $defs must be an object"
        )
    _require_supported_response_schema_node(
        document, "$", definitions, allow_tagged_unions=allow_tagged_unions
    )
    return document


def _require_supported_response_schema_node(
    node: object,
    path: str,
    definitions: dict[object, object],
    *,
    allow_tagged_unions: bool,
) -> None:
    if not isinstance(node, dict):
        raise MalformedResponsePayloadSchemaError(path)
    _require_supported_response_schema_keywords(node, path, allow_tagged_unions=allow_tagged_unions)
    if "$defs" in node and path != "$":
        raise UnsupportedResponsePayloadSchemaError(
            path, "nested response payload schema $defs are unsupported"
        )
    _require_supported_response_schema_values(
        node, path, definitions, allow_tagged_unions=allow_tagged_unions
    )
    _visit_response_schema_mapping(
        node, "$defs", path, definitions, allow_tagged_unions=allow_tagged_unions
    )
    _visit_response_schema_mapping(
        node, "properties", path, definitions, allow_tagged_unions=allow_tagged_unions
    )
    _visit_response_schema_child(
        node, "items", f"{path}[]", definitions, allow_tagged_unions=allow_tagged_unions
    )
    _visit_response_schema_choices(node, path, definitions, allow_tagged_unions=allow_tagged_unions)


def _require_supported_response_schema_keywords(
    node: dict[object, object], path: str, *, allow_tagged_unions: bool
) -> None:
    supported = (
        _RUN_EVENT_DESCRIPTOR_KEYWORDS if allow_tagged_unions else _RESPONSE_DESCRIPTOR_KEYWORDS
    )
    unsupported = sorted((key for key in node if key not in supported), key=repr)
    if unsupported:
        raise UnsupportedResponsePayloadSchemaError(
            path, f"unsupported response payload schema keyword {unsupported[0]!r}"
        )


def _require_supported_response_schema_values(
    node: dict[object, object],
    path: str,
    definitions: dict[object, object],
    *,
    allow_tagged_unions: bool,
) -> None:
    reference = node.get("$ref")
    if "$ref" in node:
        _require_supported_response_reference(reference, path, definitions)
    schema_type = node.get("type")
    if "type" in node and schema_type not in _RESPONSE_DESCRIPTOR_TYPES:
        raise UnsupportedResponsePayloadSchemaError(
            path, f"unsupported response payload schema type {schema_type!r}"
        )
    _require_supported_response_schema_composition(
        node, path, definitions, allow_tagged_unions=allow_tagged_unions
    )
    _require_supported_response_object_keywords(node, path, schema_type)
    _require_supported_response_array_keywords(node, path, schema_type)
    _require_supported_response_number_keywords(node, path, schema_type)
    _require_supported_response_string_keywords(node, path, schema_type)
    _require_supported_response_constant(node, path, schema_type)


def _require_supported_response_reference(
    reference: object, path: str, definitions: dict[object, object]
) -> None:
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        raise UnsupportedResponsePayloadSchemaError(
            path, f"unsupported response payload schema reference {reference!r}"
        )
    name = reference.removeprefix("#/$defs/")
    if not name or "/" in name or "~" in name or name not in definitions:
        raise UnsupportedResponsePayloadSchemaError(
            path, f"response payload schema reference {reference!r} does not resolve"
        )


def _require_supported_response_schema_composition(
    node: dict[object, object],
    path: str,
    definitions: dict[object, object],
    *,
    allow_tagged_unions: bool,
) -> None:
    if "oneOf" in node or "discriminator" in node:
        if not allow_tagged_unions:
            raise UnsupportedResponsePayloadSchemaError(
                path, "response payload schema tagged unions are unsupported"
            )
        _require_supported_tagged_union(node, path, definitions)
        return
    applicators = [keyword for keyword in ("$ref", "anyOf", "const") if keyword in node]
    annotation_keywords = {"default", "description", "title"}
    allowed_siblings = {"type"} if applicators == ["const"] else set()
    if applicators and (
        len(applicators) > 1
        or set(node) - annotation_keywords - allowed_siblings - {applicators[0]}
    ):
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload schema cannot combine an applicator with another shape"
        )


def _require_supported_tagged_union(
    node: dict[object, object], path: str, definitions: dict[object, object]
) -> None:
    annotations = {"default", "description", "title"}
    if set(node) - annotations - {"oneOf", "discriminator"}:
        raise UnsupportedResponsePayloadSchemaError(
            path, "tagged union cannot combine with another shape"
        )
    choices = node.get("oneOf")
    discriminator = node.get("discriminator")
    if not isinstance(choices, list) or not choices:
        raise UnsupportedResponsePayloadSchemaError(
            path, "tagged union oneOf must be a nonempty array"
        )
    if not isinstance(discriminator, dict):
        raise UnsupportedResponsePayloadSchemaError(path, "tagged union needs a discriminator")
    property_name = discriminator.get("propertyName")
    mapping = discriminator.get("mapping")
    if not isinstance(property_name, str) or not property_name or not isinstance(mapping, dict):
        raise UnsupportedResponsePayloadSchemaError(
            path, "tagged union discriminator needs a propertyName and mapping"
        )
    members = _require_tagged_union_reference_bijection(choices, mapping, path, definitions)
    for tag, ref in members:
        _require_tagged_union_mapping_tag(tag, ref, property_name, path, definitions)


def _require_tagged_union_reference_bijection(
    choices: list[object],
    mapping: dict[object, object],
    path: str,
    definitions: dict[object, object],
) -> list[tuple[str, str]]:
    """Return a proven one-to-one mapping between union tags and member refs."""
    members: list[tuple[str, str]] = []
    for tag, ref in mapping.items():
        if not isinstance(tag, str) or not isinstance(ref, str):
            raise UnsupportedResponsePayloadSchemaError(
                path, "tagged union mapping must map strings to references"
            )
        members.append((tag, ref))

    choice_refs = [choice.get("$ref") if isinstance(choice, dict) else None for choice in choices]
    if any(not isinstance(ref, str) for ref in choice_refs):
        raise UnsupportedResponsePayloadSchemaError(path, "tagged union members must be references")
    references = [ref for ref in choice_refs if isinstance(ref, str)]
    if len(set(references)) != len(references):
        raise UnsupportedResponsePayloadSchemaError(
            path, "tagged union members must contain unique references"
        )
    mapped_references = [ref for _, ref in members]
    if len(set(mapped_references)) != len(mapped_references):
        raise UnsupportedResponsePayloadSchemaError(
            path, "tagged union mapping must contain unique references"
        )
    if len(members) != len(references) or set(mapped_references) != set(references):
        raise UnsupportedResponsePayloadSchemaError(
            path, "tagged union mapping must name every member exactly once"
        )
    for ref in mapped_references:
        _require_supported_response_reference(ref, path, definitions)
    return members


def _require_tagged_union_mapping_tag(
    tag: str,
    reference: str,
    property_name: str,
    path: str,
    definitions: dict[object, object],
) -> None:
    """Require a mapping key to equal its member's literal discriminator."""
    member = definitions[reference.removeprefix("#/$defs/")]
    properties = member.get("properties") if isinstance(member, dict) else None
    property_schema = properties.get(property_name) if isinstance(properties, dict) else None
    constant = property_schema.get("const") if isinstance(property_schema, dict) else None
    if not isinstance(constant, str):
        raise UnsupportedResponsePayloadSchemaError(
            path,
            f"tagged union member {reference!r} needs a string const for {property_name!r}",
        )
    if tag != constant:
        raise UnsupportedResponsePayloadSchemaError(
            path,
            f"tagged union mapping key {tag!r} does not match "
            f"{reference}.{property_name} const {constant!r}",
        )


def _require_supported_response_object_keywords(
    node: dict[object, object], path: str, schema_type: object
) -> None:
    required = node.get("required")
    additional = node.get("additionalProperties")
    object_keywords = {"properties", "required", "additionalProperties"}
    if set(node) & object_keywords and schema_type != "object":
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload object keywords require type 'object'"
        )
    if "required" in node and (
        not isinstance(required, list)
        or any(not isinstance(name, str) for name in required)
        or len(set(required)) != len(required)
    ):
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload schema required must contain unique strings"
        )
    if "additionalProperties" in node and not isinstance(additional, bool):
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload schema additionalProperties must be boolean"
        )


def _require_supported_response_array_keywords(
    node: dict[object, object], path: str, schema_type: object
) -> None:
    if "items" in node and schema_type != "array":
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload schema items requires type 'array'"
        )


def _require_supported_response_number_keywords(
    node: dict[object, object], path: str, schema_type: object
) -> None:
    minimum = node.get("minimum")
    if "minimum" not in node:
        return
    if schema_type not in {"integer", "number"}:
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload schema minimum requires a numeric type"
        )
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, (int, float))
        or not math.isfinite(minimum)
    ):
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload schema minimum must be a finite number"
        )


def _require_supported_response_string_keywords(
    node: dict[object, object], path: str, schema_type: object
) -> None:
    string_constraints = {"minLength", "maxLength", "pattern"}
    if set(node) & string_constraints and schema_type != "string":
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload string constraints require type 'string'"
        )
    for keyword in ("minLength", "maxLength"):
        value = node.get(keyword)
        if keyword in node and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise UnsupportedResponsePayloadSchemaError(
                path,
                f"response payload schema {keyword} must be a nonnegative integer",
            )
    minimum_length = node.get("minLength")
    maximum_length = node.get("maxLength")
    if (
        isinstance(minimum_length, int)
        and not isinstance(minimum_length, bool)
        and isinstance(maximum_length, int)
        and not isinstance(maximum_length, bool)
        and minimum_length > maximum_length
    ):
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload schema minLength cannot exceed maxLength"
        )
    pattern = node.get("pattern")
    if "pattern" in node:
        if not isinstance(pattern, str):
            raise UnsupportedResponsePayloadSchemaError(
                path, "response payload schema pattern must be a string"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise UnsupportedResponsePayloadSchemaError(
                path, "response payload schema pattern must be a valid regular expression"
            ) from exc
        _require_portable_client_pattern(pattern, path)
    schema_format = node.get("format")
    if "format" in node and (schema_type != "string" or schema_format != "date-time"):
        raise UnsupportedResponsePayloadSchemaError(
            path, f"unsupported response payload schema format {schema_format!r}"
        )
    enum = node.get("enum")
    if "enum" in node and (
        schema_type != "string"
        or not isinstance(enum, list)
        or not enum
        or any(not isinstance(member, str) for member in enum)
    ):
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload schema enum must contain one or more strings"
        )


def _require_portable_client_pattern(pattern: str, path: str) -> None:
    """Restrict patterns to syntax shared by Python and ECMAScript Unicode regexes."""
    _reject_quantified_client_assertions(pattern, path)
    index = 0
    in_character_class = False
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            index = _portable_client_escape_end(
                pattern,
                index,
                path,
                in_character_class=in_character_class,
            )
            continue
        if in_character_class:
            in_character_class = character != "]"
            index += 1
            continue
        index, in_character_class = _portable_client_expression_token_end(pattern, index, path)


def _reject_quantified_client_assertions(pattern: str, path: str) -> None:
    """Reject lookaheads quantified only by legacy, non-Unicode ECMAScript."""
    groups: list[bool] = []
    index = 0
    in_character_class = False
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            index += 2
            continue
        if in_character_class:
            in_character_class = character != "]"
            index += 1
            continue
        if character == "[":
            in_character_class = True
        elif character == "(":
            groups.append(pattern[index + 1 : index + 3] in {"?=", "?!"})
        elif character == ")":
            assertion = groups.pop()
            following = pattern[index + 1 : index + 2]
            if assertion and (
                following in {"*", "+", "?"}
                or (
                    following == "{"
                    and _portable_client_quantifier_end(pattern, index + 1) is not None
                )
            ):
                _raise_nonportable_client_pattern(path)
        index += 1


def _portable_client_expression_token_end(
    pattern: str,
    index: int,
    path: str,
) -> tuple[int, bool]:
    """Validate one token outside a character class."""
    character = pattern[index]
    if character == "[":
        return index + 1, True
    if character in "]}":
        _raise_nonportable_client_pattern(path)
    if character == "{":
        quantifier_end = _portable_client_quantifier_end(pattern, index)
        if quantifier_end is None or pattern[quantifier_end : quantifier_end + 1] == "+":
            _raise_nonportable_client_pattern(path)
        return quantifier_end, False
    if (
        character == "("
        and pattern[index + 1 : index + 2] == "?"
        and pattern[index + 1 : index + 3] not in _CLIENT_PATTERN_GROUP_PREFIXES
    ):
        _raise_nonportable_client_pattern(path)
    if character in "*+?" and pattern[index + 1 : index + 2] == "+":
        # Python's possessive quantifiers are not part of the Node 20 RegExp
        # grammar used by the generated client.
        _raise_nonportable_client_pattern(path)
    return index + 1, False


def _portable_client_quantifier_end(pattern: str, index: int) -> int | None:
    """Return the position after one shared ``{m,n}`` quantifier."""
    cursor = index + 1
    digit_start = cursor
    while pattern[cursor : cursor + 1].isdigit():
        cursor += 1
    if cursor == digit_start:
        return None
    if pattern[cursor : cursor + 1] == ",":
        cursor += 1
        while pattern[cursor : cursor + 1].isdigit():
            cursor += 1
    if pattern[cursor : cursor + 1] != "}":
        return None
    return cursor + 1


def _portable_client_escape_end(
    pattern: str,
    index: int,
    path: str,
    *,
    in_character_class: bool,
) -> int:
    escaped = pattern[index + 1 : index + 2]
    if escaped in {"x", "u"}:
        width = 2 if escaped == "x" else 4
        digits = pattern[index + 2 : index + 2 + width]
        if len(digits) != width or any(
            character not in "0123456789abcdefABCDEF" for character in digits
        ):
            _raise_nonportable_client_pattern(path)
        return index + 2 + width
    if escaped == "-" and in_character_class:
        return index + 2
    if escaped not in _CLIENT_PATTERN_SIMPLE_ESCAPES:
        _raise_nonportable_client_pattern(path)
    return index + 2


def _raise_nonportable_client_pattern(path: str) -> Never:
    raise UnsupportedResponsePayloadSchemaError(
        path,
        "response payload schema pattern must use the portable client regular-expression dialect",
    )


def _require_supported_response_constant(
    node: dict[object, object], path: str, schema_type: object
) -> None:
    if "const" not in node:
        return
    constant = node["const"]
    if isinstance(constant, (dict, list)):
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload schema const must be a scalar"
        )
    if isinstance(constant, float) and not math.isfinite(constant):
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload schema const must be finite"
        )
    if "type" not in node:
        return
    compatible = {
        "boolean": isinstance(constant, bool),
        "integer": isinstance(constant, int) and not isinstance(constant, bool),
        "null": constant is None,
        "number": isinstance(constant, (int, float)) and not isinstance(constant, bool),
        "string": isinstance(constant, str),
    }.get(schema_type, False)
    if not compatible:
        raise UnsupportedResponsePayloadSchemaError(
            path, "response payload schema const does not match its type"
        )


def _visit_response_schema_mapping(
    node: dict[object, object],
    keyword: str,
    path: str,
    definitions: dict[object, object],
    *,
    allow_tagged_unions: bool,
) -> None:
    children = node.get(keyword)
    if keyword not in node:
        return
    if not isinstance(children, dict):
        raise UnsupportedResponsePayloadSchemaError(
            path, f"response payload schema {keyword} must be an object"
        )
    for name, child in children.items():
        _require_supported_response_schema_node(
            child,
            f"{path}.{keyword}.{name}",
            definitions,
            allow_tagged_unions=allow_tagged_unions,
        )


def _visit_response_schema_child(
    node: dict[object, object],
    keyword: str,
    path: str,
    definitions: dict[object, object],
    *,
    allow_tagged_unions: bool,
) -> None:
    child = node.get(keyword)
    if keyword not in node:
        return
    _require_supported_response_schema_node(
        child, path, definitions, allow_tagged_unions=allow_tagged_unions
    )


def _visit_response_schema_choices(
    node: dict[object, object],
    path: str,
    definitions: dict[object, object],
    *,
    allow_tagged_unions: bool,
) -> None:
    for keyword in ("anyOf", "oneOf"):
        choices = node.get(keyword)
        if keyword not in node:
            continue
        if not isinstance(choices, list) or not choices:
            raise UnsupportedResponsePayloadSchemaError(
                path, f"response payload schema {keyword} must be a nonempty array"
            )
        for index, choice in enumerate(choices):
            _require_supported_response_schema_node(
                choice,
                f"{path}.{keyword}[{index}]",
                definitions,
                allow_tagged_unions=allow_tagged_unions,
            )


def fully_populated_response() -> Response:
    """Build one server-owned response carrying every client payload model.

    The generated fixture is a cross-language conformance input. It uses
    ``Response.model_validate`` so every nested payload is a real Pydantic
    model before ``model_dump_json`` serializes the exact server wire form.
    """
    return Response.model_validate(
        {
            "request_id": "response-payload-fixture",
            "timestamp": "2026-01-01T00:00:00Z",
            "ack": {"action": "pause", "status": "pending"},
            "chat": {"question": "q", "answer": "a"},
            "chat_thread": {
                "thread_id": "thread",
                "title": "A thread title",
                "provider": "provider",
                "model": "model",
            },
            "snapshot": {
                "run_id": "run",
                "sequence": 1,
                "status": "running",
                "agent_kind": "agent",
                "round_label": "round-1",
                "active_executions": [
                    {
                        "execution_id": "execution",
                        "agent_kind": "agent",
                        "round_label": "round-1",
                        "stage": "implement",
                        "attempt": 1,
                        "assignment": "assignment",
                        "started_at": "2026-01-01T00:00:00Z",
                        "activity": {
                            "kind": "agent_execution_activity_changed",
                            "mode": "thinking",
                            "summary": "working",
                            "tool": "tool",
                        },
                        "provider": "provider",
                        "model": "model",
                    }
                ],
                "chat_threads": [
                    {
                        "thread_id": "thread",
                        "title": "A thread title",
                        "provider": "provider",
                        "model": "model",
                    }
                ],
            },
            "performance": [
                {
                    "round": 1,
                    "perf_metric": 1.0,
                    "perf_unit": "s",
                    "passed": True,
                    "profile_skipped": True,
                }
            ],
            "performance_context": {
                "objective_metric": "metric",
                "objective_unit": "s",
                "objective_direction": "min",
                "objective_baseline_value": 2.0,
                "objective_baseline_round": 0,
                "objective_baseline_commit": "baseline",
                "objective_description": "description",
            },
            "experiments": [
                {
                    "hypothesis_id": "hypothesis",
                    "identified": True,
                    "title": "title",
                    "claim": "claim",
                    "action": "action",
                    "first_round": 1,
                    "last_round": 1,
                    "rounds": [
                        {
                            "round": 1,
                            "passed": True,
                            "reviewed": True,
                            "hypothesis_outcome": "supported",
                            "judge_verdict": "pass",
                            "perf_metric": 1.0,
                            "perf_unit": "s",
                            "perf_delta_pct": 5.0,
                            "commit": "commit",
                            "official_evaluation": True,
                            "candidate_disposition": "pareto_frontier",
                        }
                    ],
                    "resolved_outcome": "proven",
                    "judge_verdict": "pass",
                    "perf_metric": 1.0,
                    "perf_unit": "s",
                    "perf_delta_pct": 5.0,
                    "perf_metric_name": "metric",
                    "perf_direction": "min",
                    "perf_baseline_value": 2.0,
                    "perf_baseline_round": 0,
                    "perf_baseline_commit": "baseline",
                    "perf_delta_reason": "no_baseline_yet",
                    "best": True,
                    "strategy_disposition": "available",
                    "strategy_reason": "reason",
                    "active": True,
                }
            ],
            "experiment_update": {
                "run_id": "run",
                "projection_id": "projection",
                "from_revision": 0,
                "through_revision": 1,
                "reset": True,
                "removed_hypothesis_ids": ["removed"],
            },
            "experiments_ready": True,
            "iterations": IterationSlice(
                run_id="run",
                projection_revision="iteration-revision",
                parent_id=None,
                items=(
                    IterationSummary(
                        id="round:1",
                        kind=IterationKind.ROUND,
                        label="Round 1",
                        ordinal=1,
                        status=IterationStatus.COMPLETED,
                        headline_metrics=(
                            IterationMetric(
                                id="latency:p99",
                                value=1.0,
                                unit="s",
                                direction="min",
                            ),
                        ),
                    ),
                ),
            ),
            "iterations_ready": True,
            "design": [
                {
                    "round": 1,
                    "commit": "commit",
                    "base": "base",
                    "files": [
                        {
                            "path": "file.py",
                            "change": "renamed",
                            "renamed_from": "old.py",
                        }
                    ],
                }
            ],
            "design_ready": True,
            "design_patch": {
                "base": "base",
                "head": "head",
                "path": "file.py",
                "renamed_from": "old.py",
                "patch": "diff",
                "truncated": True,
            },
        }
    )


def _referenced_definitions(definitions: dict[str, Any], root: object) -> set[str]:
    """Return definitions reachable from ``root`` through local references."""
    pending = list(_schema_references(root))
    reached: set[str] = set()
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        if name not in definitions:
            message = f"response payload schema references missing definition {name!r}"
            raise ValueError(message)
        reached.add(name)
        pending.extend(_schema_references(definitions[name]))
    return reached


def _schema_references(node: object) -> Iterator[str]:
    """Yield definition names from local schema references beneath ``node``."""
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            yield reference.removeprefix("#/$defs/")
        for value in node.values():
            yield from _schema_references(value)
    elif isinstance(node, list):
        for value in node:
            yield from _schema_references(value)


def require_tagged_union_discriminants(document: dict[str, Any]) -> dict[str, Any]:
    """Return ``document`` with every tagged union's discriminant required.

    Pydantic omits a field from ``required`` whenever it has a default, and
    every discriminant carries its own tag as that default. A tagged union
    nonetheless rejects input that omits the discriminator
    (``union_tag_not_found``), so the raw export describes a contract the
    server never accepts, and generated clients get optional tags they cannot
    narrow on. Requiring the discriminant states the guarantee the validator
    already enforces.

    A union is recognized by the OpenAPI ``discriminator`` keyword Pydantic
    emits beside ``oneOf``, so the tag names and their member schemas come from
    the document itself rather than a hand-kept list.

    ``document`` is not modified. Unknown or unresolvable union shapes raise
    instead of being skipped, because silently leaving one union untagged is
    exactly the defect this transform exists to prevent.

    Raises:
        ValueError: if a tagged union cannot be interpreted, naming the
            offending schema path.
    """
    result = copy.deepcopy(document)
    # Collect before rewriting: adding a ``required`` key to a member that had
    # none would otherwise resize a dict the walk may still be iterating.
    for path, discriminator, members in list(_tagged_unions(result, "")):
        _require_discriminant(result, path, discriminator, members)
    return result


def _tagged_unions(
    node: object,
    path: str,
) -> Iterator[tuple[str, dict[str, Any], list[Any]]]:
    """Yield each OpenAPI tagged union under ``node`` with its schema path."""
    if isinstance(node, dict):
        discriminator = node.get("discriminator")
        one_of = node.get("oneOf")
        if isinstance(discriminator, dict) and isinstance(one_of, list):
            yield path, discriminator, one_of
        for key, value in node.items():
            yield from _tagged_unions(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _tagged_unions(value, f"{path}/{index}")


def _require_discriminant(
    document: dict[str, Any],
    path: str,
    discriminator: dict[str, Any],
    members: list[Any],
) -> None:
    """Mark one union's discriminant required on every member, in place."""
    match discriminator:
        case {"propertyName": str(property_name), "mapping": dict(mapping)}:
            pass
        case _:
            message = f"{path}: tagged union needs a propertyName and a mapping to rewrite"
            raise ValueError(message)
    if len(mapping) != len(members):
        message = f"{path}: tagged union maps {len(mapping)} tags for {len(members)} members"
        raise ValueError(message)
    for tag, ref in mapping.items():
        member_path = f"{path}/discriminator/mapping/{tag}"
        member = _resolve_ref(document, ref, member_path)
        _mark_required(member, property_name, member_path)


def _resolve_ref(document: dict[str, Any], ref: object, path: str) -> dict[str, Any]:
    """Resolve a local JSON pointer, or fail naming the unresolvable path."""
    if not isinstance(ref, str) or not ref.startswith(_LOCAL_REF_PREFIX):
        message = f"{path}: expected a local schema reference, got {ref!r}"
        raise ValueError(message)
    target: object = document
    for token in ref.removeprefix(_LOCAL_REF_PREFIX).split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or key not in target:
            message = f"{path}: schema reference {ref!r} does not resolve"
            raise ValueError(message)
        target = target[key]
    match target:
        case dict():
            return target
        case _:
            message = f"{path}: schema reference {ref!r} does not name an object schema"
            raise ValueError(message)


def _mark_required(member: dict[str, Any], property_name: str, path: str) -> None:
    """Add ``property_name`` to ``member``'s required list, in declaration order."""
    properties = member.get("properties")
    if not isinstance(properties, dict) or property_name not in properties:
        message = f"{path}: union member declares no {property_name!r} property"
        raise ValueError(message)
    required = {*member.get("required", ()), property_name}
    unknown = required - set(properties)
    if unknown:
        message = f"{path}: required names absent from properties: {sorted(unknown)}"
        raise ValueError(message)
    # Re-derive from ``properties`` so the list keeps Pydantic's own
    # field-declaration order and the exported bytes stay stable.
    member["required"] = [key for key in properties if key in required]


def main(argv: Sequence[str] | None = None) -> None:
    """Write the public protocol JSON schema to the requested path."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) == _OPTION_ARGUMENT_COUNT and arguments[0] == "--host-helper-schema":
        document, output = host_helper_json_schema(), Path(arguments[1])
    elif len(arguments) == _OPTION_ARGUMENT_COUNT and arguments[0] == "--response-payload-schema":
        document, output = response_payload_json_schema(), Path(arguments[1])
    elif len(arguments) == _OPTION_ARGUMENT_COUNT and arguments[0] == "--response-payload-module":
        output = Path(arguments[1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(response_payload_typescript())
        return
    elif len(arguments) == _OPTION_ARGUMENT_COUNT and arguments[0] == "--response-payload-fixture":
        output = Path(arguments[1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(fully_populated_response().model_dump_json() + "\n")
        return
    elif len(arguments) == _OPTION_ARGUMENT_COUNT and arguments[0] == "--host-helper-module":
        output = Path(arguments[1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(host_helper_typescript())
        return
    elif len(arguments) == _OPTION_ARGUMENT_COUNT and arguments[0] == "--display-names-module":
        output = Path(arguments[1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(display_names_typescript())
        return
    elif len(arguments) == _OPTION_ARGUMENT_COUNT and arguments[0] == "--run-event-module":
        output = Path(arguments[1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(run_event_typescript())
        return
    else:
        message = (
            "usage: python -m server.api.schema "
            "(--host-helper-schema|--host-helper-module|--response-payload-schema|"
            "--response-payload-module|"
            "--response-payload-fixture|--run-event-module|--display-names-module) OUTPUT; the protocol schema itself "
            "is written by python -m entrypoints.protocol_schema OUTPUT"
        )
        raise SystemExit(message)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2) + "\n")


if __name__ == "__main__":
    main()
