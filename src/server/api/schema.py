"""Export the wire contract as JSON Schema for generated clients."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from server.api.protocol import ProtocolRequest, Response, RunSnapshot, ServerMessage
from server.events import RunEvent

if TYPE_CHECKING:
    from collections.abc import Iterator

_EXPECTED_ARGUMENT_COUNT = 2
_LOCAL_REF_PREFIX = "#/"


class ProtocolDocument(BaseModel):
    """Root schema document for the public server protocol."""

    request: ProtocolRequest
    response: Response
    event: RunEvent
    snapshot: RunSnapshot
    server_message: ServerMessage


def protocol_json_schema() -> dict[str, Any]:
    """Return the published JSON Schema document for the server protocol.

    This is the authoritative export. The committed
    ``protocol.schema.json`` artifact and every generated client come from
    here, not from ``ProtocolDocument.model_json_schema()`` directly.
    """
    return require_tagged_union_discriminants(ProtocolDocument.model_json_schema())


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


def main() -> None:
    """Write the public protocol JSON schema to the requested path."""
    if len(sys.argv) != _EXPECTED_ARGUMENT_COUNT:
        message = "usage: python -m server.api.schema OUTPUT.json"
        raise SystemExit(message)
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(protocol_json_schema(), indent=2) + "\n")


if __name__ == "__main__":
    main()
