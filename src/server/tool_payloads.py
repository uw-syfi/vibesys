"""Best-effort classification of raw tool-result text into typed payloads.

Producers that received real structure attach a typed payload themselves;
this module is the ONLY place allowed to guess structure from flattened
text, and it guesses conservatively: strict JSON, objects and arrays only.
"""

from __future__ import annotations

import json

from vs_agent.api import JsonResultPayload, ToolResultPayload


def classify_tool_result(content: str) -> ToolResultPayload | None:
    """Return a typed payload for *content* when it is a JSON object or array.

    Scalars, text with trailing garbage, and anything that fails strict
    parsing stay unclassified so the raw text renders unchanged.
    """
    trimmed = content.strip()
    if not trimmed.startswith(("{", "[")):
        return None
    try:
        value = json.loads(trimmed)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, (dict, list)):
        return None
    return JsonResultPayload(value=value)
