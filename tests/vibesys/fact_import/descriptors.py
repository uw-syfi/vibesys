"""Generate the frozen descriptors of the current envelope version from the live models.

Run ``uv run --locked python -m tests.vibesys.fact_import.descriptors`` after bumping
``ENVELOPE_SCHEMA_VERSION``: it writes ``envelope-v<N>.json`` for the new version and leaves
every older entry, and ``retained.json``, untouched. Pin the previous version's file by hash
in ``test_descriptors.py``. A compatible change keeps the version
(``scripts/check_persisted_schema_compat.py``): run it again then, and the current version's
file widens to the new shape, which every older document of that version still satisfies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from vs_agent.api import AgentSessionState
from vs_core.api import ENVELOPE_SCHEMA_VERSION, FlowState, OperationResult, RunEnvelope
from vs_runtime.api.core import PublicationHistory, RuntimeRecord

if TYPE_CHECKING:
    from pydantic import JsonValue

DIRECTORY = Path(__file__).parents[3] / "src" / "vibesys" / "fact_import" / "descriptors"
_ANNOTATIONS = frozenset({"description", "title", "default", "examples"})
_NAMED_CHILDREN = frozenset(
    {"properties", "$defs", "patternProperties", "dependentSchemas", "mapping"}
)
_LIVE = {
    "shared:flow": FlowState,
    "shared:envelope": RunEnvelope[FlowState],
    "shared:runtime": RuntimeRecord[FlowState],
    "shared:publications": PublicationHistory,
    "shared:operation-result": OperationResult,
    "shared:sessions": AgentSessionState,
}


def strip(value: JsonValue) -> JsonValue:
    """Drop annotations validation ignores; a property or definition named ``title`` stays."""
    if isinstance(value, dict):
        return {
            key: (
                {name: strip(child) for name, child in item.items()}
                if key in _NAMED_CHILDREN and isinstance(item, dict)
                else strip(item)
            )
            for key, item in value.items()
            if key not in _ANNOTATIONS
        }
    if isinstance(value, list):
        return [strip(item) for item in value]
    return value


def live_descriptors() -> dict[str, JsonValue]:
    """The shared framework descriptors exactly as today's models validate them."""
    return {key: strip(TypeAdapter(model).json_schema()) for key, model in _LIVE.items()}


def render(descriptors: dict[str, JsonValue]) -> str:
    """One sorted compact descriptor per line, so a diff names the descriptor it changes."""
    rows = (
        f"{json.dumps(key)}:{json.dumps(value, sort_keys=True, separators=(',', ':'))}"
        for key, value in sorted(descriptors.items())
    )
    return "{\n" + ",\n".join(rows) + "\n}\n"


def current_path() -> Path:
    return DIRECTORY / f"envelope-v{ENVELOPE_SCHEMA_VERSION}.json"


if __name__ == "__main__":
    current_path().write_text(render(live_descriptors()))
