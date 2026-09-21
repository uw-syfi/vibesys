"""Typed-response recovery from raw agent text."""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


def parse_typed_response_text(text: str, response_cls: type[T]) -> T | None:
    """Best-effort recovery of a typed Pydantic payload from raw model text."""
    if not text:
        return None

    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)

    fenced_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates.extend(match.strip() for match in fenced_matches if match.strip())

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1].strip())

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            payload = json.loads(candidate)
            return response_cls.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, TypeError):
            continue
    return None
