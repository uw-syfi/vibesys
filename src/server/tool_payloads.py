"""Best-effort classification of raw tool-result text into typed payloads.

Producers that received real structure attach a typed payload themselves;
this module is the ONLY place allowed to guess structure from flattened
text, and it guesses conservatively: strict JSON, objects and arrays only.
"""

from __future__ import annotations

import json

from google.protobuf import json_format

from server.wire.v2 import events_pb2


def _reject_constant(name: str) -> None:
    """Refuse ``NaN`` and ``Infinity``: they are not JSON and not finite."""
    raise ValueError(name)


def classify_tool_result(content: str) -> events_pb2.JsonResultPayload | None:
    """Return a JSON payload for *content* when it is a JSON object or array.

    Scalars, text with trailing garbage, and anything that fails strict
    parsing stay unclassified so the raw text renders unchanged.
    """
    trimmed = content.strip()
    if not trimmed.startswith(("{", "[")):
        return None
    try:
        value = json.loads(trimmed, parse_constant=_reject_constant)
    except ValueError:
        return None
    if not isinstance(value, (dict, list)):
        return None
    payload = events_pb2.JsonResultPayload()
    json_format.ParseDict(value, payload.value)
    return payload
