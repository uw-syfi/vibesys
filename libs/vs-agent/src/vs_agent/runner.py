"""Typed-response recovery from raw agent text."""

from __future__ import annotations

import json
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

    The reply is the last complete JSON value in *text* that validates, so a
    draft the agent corrected, or braces in its prose, never hide it. Raises
    ``AgentOutputSchemaError`` naming the offending fields of the closest
    candidate, or saying that the reply held no JSON object at all.
    """
    failures: list[ValidationError | TypeError] = []
    for candidate in _candidates(text):
        try:
            return response_cls.model_validate_json(candidate)
        except (ValidationError, TypeError) as error:
            failures.append(error)
    raise AgentOutputSchemaError(_closest_failure(failures))


def parse_typed_response_text(text: str, response_cls: type[T]) -> T | None:
    """Like ``parse_typed_response``, but ``None`` when no JSON value validates."""
    try:
        return parse_typed_response(text, response_cls)
    except AgentOutputSchemaError:
        return None


def _closest_failure(failures: list[ValidationError | TypeError]) -> str:
    """Describe the failure nearest to valid: fewest errors, later wins ties, ``{}`` last."""
    if not failures:
        return _NO_JSON_OBJECT

    def distance(failure: ValidationError | TypeError) -> tuple[bool, int]:
        if isinstance(failure, TypeError):
            return (True, 0)
        errors = failure.errors(include_url=False)
        # An empty object (prose such as "replaced {} with a list") misses every
        # required field, so it never stands in for the agent's own reply.
        empty = all(item["type"] == "missing" and item["input"] == {} for item in errors)
        return (empty, len(errors))

    closest = min(reversed(failures), key=distance)
    if isinstance(closest, TypeError):
        return str(closest)
    return describe_validation_error(closest)


def _candidates(text: str) -> list[str]:
    """Complete JSON values in *text*, last first: each outermost ``{...}``, then the whole text.

    ``raw_decode`` at every ``{`` finds an object by its syntax, not by the
    position of the last ``}``; a value that decodes is skipped over, so an
    object's own members are not candidates. A non-object root (the whole text
    is one JSON array, say) is tried last.
    """
    candidates: list[str] = []
    decoder = json.JSONDecoder()
    position = text.find("{")
    while position != -1:
        try:
            _, end = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            position = text.find("{", position + 1)
            continue
        candidates.append(text[position:end])
        position = text.find("{", end)
    candidates.reverse()
    whole = text.strip()
    if whole and whole not in candidates and _is_json(whole):
        candidates.append(whole)
    return candidates


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True
