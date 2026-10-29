"""Write the run protocol's JSON schema with the built-in orchestrations' span kinds.

The server owns the protocol document; which orchestrations exist is the product catalog's
fact. This composition root joins the two, so the generated schema (and the TypeScript built
from it) carries each declared kind's attribute schema:
``python -m entrypoints.protocol_schema <output.json>``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from launch import built_in_orchestrations
from server.api.schema import SpanAttributeModels, protocol_json_schema

if TYPE_CHECKING:
    from pydantic import BaseModel


def built_in_span_attributes() -> SpanAttributeModels:
    """For each built-in orchestration that labels spans, each kind's attribute model."""
    found: dict[str, dict[str, type[BaseModel]]] = {}
    for registration in built_in_orchestrations().registrations():
        plugin = registration.plugin
        kinds = {kind.name: kind.attrs for kind in plugin.core.span_kinds if kind.attrs}
        if kinds:
            found[plugin.id] = kinds
    return found


def main() -> None:
    """Write the schema to the path given as the only argument."""
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    document = protocol_json_schema(built_in_span_attributes())
    output.write_text(json.dumps(document, indent=2) + "\n")


if __name__ == "__main__":
    main()
