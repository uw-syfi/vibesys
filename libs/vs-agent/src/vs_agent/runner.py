"""Typed-response recovery from raw agent text."""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from vs_agent.contracts import AgentOutputSchemaError

T = TypeVar("T", bound=BaseModel)

_NO_JSON_OBJECT = "the reply contained no JSON object"


def describe_validation_error(error: ValidationError) -> str:
    """Name each offending field and say what is wrong with it, one error per clause.

    This is the text an agent is shown to correct its reply, so it carries
    the field path and the validator's message, not pydantic's links.
    """
    clauses: list[str] = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "root"
        clauses.append(f"{location}: {item['msg']}")
    return "; ".join(clauses)


def validate_typed_response(payload: object, response_cls: type[T]) -> T:
    """Validate one decoded payload, raising ``AgentOutputSchemaError`` with field-named errors."""
    try:
        return response_cls.model_validate(payload)
    except ValidationError as error:
        raise AgentOutputSchemaError(describe_validation_error(error)) from error


def parse_typed_response(text: str, response_cls: type[T]) -> T:
    """Recover a typed payload from raw model text, or say why none validates.

    Raises ``AgentOutputSchemaError`` whose detail names the offending fields
    of the last JSON object found, or says that the reply held no JSON object.
    """
    parsed = parse_typed_response_text(text, response_cls)
    if parsed is not None:
        return parsed
    detail = _NO_JSON_OBJECT
    for candidate in _candidates(text):
        try:
            json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        try:
            response_cls.model_validate_json(candidate)
        except ValidationError as error:
            detail = describe_validation_error(error)
        except TypeError as error:
            detail = str(error)
    raise AgentOutputSchemaError(detail)


def parse_typed_response_text(text: str, response_cls: type[T]) -> T | None:
    """Best-effort recovery of a typed Pydantic payload from raw model text."""
    for candidate in _candidates(text):
        try:
            return response_cls.model_validate_json(candidate)
        except (json.JSONDecodeError, ValidationError, TypeError):
            continue
    return None


def _candidates(text: str) -> list[str]:
    """The distinct JSON-object candidates in *text*: whole, fenced, then outermost braces."""
    if not text:
        return []
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
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))
