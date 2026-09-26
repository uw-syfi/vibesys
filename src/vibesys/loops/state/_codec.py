"""Pure helpers for stable JSON-compatible state codecs."""

from __future__ import annotations

import json
from typing import Any, NoReturn, TypeVar

from pydantic import BaseModel

_ModelT = TypeVar("_ModelT", bound=BaseModel)
JsonObject = dict[str, Any]


def invalid_state(message: str) -> NoReturn:
    """Raise a value error that Pydantic reports as model validation failure."""
    raise ValueError(message)


def parse_json_object(model_type: type[_ModelT], data: JsonObject) -> _ModelT:
    """Validate one JSON-compatible object with JSON's native type rules."""
    encoded = json.dumps(data, allow_nan=False, separators=(",", ":"))
    return model_type.model_validate_json(encoded)


def serialize_json_object(model: BaseModel) -> JsonObject:
    """Return the stable JSON-compatible object representation of *model*."""
    return model.model_dump(mode="json")
