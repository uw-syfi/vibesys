"""Registered outcome ingress shared by observation, callback and durable intent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._values import canonical_json
from .types.common import ExecuteRegisteredOperation

if TYPE_CHECKING:
    from pydantic import BaseModel, ValidationInfo


class OutcomeCodecError(ValueError):
    """Typed owner payload cannot be narrowed or decoded without its registry."""

    def __init__(self, reason: str) -> None:
        details = {
            "schema": "requires operation_schema",
            "context": "context required",
            "conflict": "wire payload conflicts with typed value",
            "mismatch": "outcome_schema mismatch",
        }
        super().__init__(f"outcome: registered codec {details[reason]}")


def bind_outcome[M: BaseModel](value: M, info: ValidationInfo) -> M:
    """Derive the owning subtype after strict JSON contract validation."""
    schema = getattr(value, "operation_schema", None)
    request = getattr(value, "request", None)
    if schema is None and isinstance(request, ExecuteRegisteredOperation):
        schema = request.operation.schema_ref
    payload = getattr(value, "outcome_json", None)
    outcome = getattr(value, "outcome", None)
    if schema is None:
        if outcome is not None:
            raise OutcomeCodecError("schema")
        return value
    registry = (info.context or {}).get("operation_registry")
    if registry is None:
        return _without_registry(value, payload, outcome)
    if outcome is not None:
        encoded = registry.encode_outcome(schema, outcome)
        if payload is not None and registry.decode_outcome(schema, payload) != outcome:
            raise OutcomeCodecError("conflict")
        payload = encoded
    if payload is None:
        return value
    decoded = registry.decode_outcome(schema, payload)
    if getattr(value, "outcome_schema", None) not in (None, schema.outcome_schema):
        raise OutcomeCodecError("mismatch")
    return value.model_copy(update={"outcome": decoded, "outcome_json": canonical_json(decoded)})


def _without_registry[M: BaseModel](value: M, payload: str | None, outcome: BaseModel | None) -> M:
    if outcome is not None:
        return value
    if payload is not None or getattr(value, "kind", None) == "operation_result":
        raise OutcomeCodecError("context")
    return value
