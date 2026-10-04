"""Registered outcome ingress shared by observation, callback and durable intent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, PrivateAttr

from ._values import canonical_json
from .types.common import ExecuteRegisteredOperation, OperationSchemaRef, Value

if TYPE_CHECKING:
    from pydantic import ValidationInfo


class OutcomeValue(Value):
    """Value-only codec proof travels with observations and callback values."""

    _outcome_proof: BaseModel | None = PrivateAttr(default=None)
    _schema_proof: OperationSchemaRef | None = PrivateAttr(default=None)

    @property
    def outcome_is_registered(self) -> bool:
        """A copied or unregistered payload cannot reuse validated wire identity."""
        outcome = getattr(self, "outcome", None)
        return (
            self._schema_proof is not None
            and self._outcome_proof is not None
            and type(outcome) is type(self._outcome_proof)
            and outcome == self._outcome_proof
            and getattr(self, "outcome_json", None) == canonical_json(self._outcome_proof)
            and _schema(self) == self._schema_proof
            and getattr(self, "outcome_schema", None) in (None, self._schema_proof.outcome_schema)
        )


def _schema(value: BaseModel) -> OperationSchemaRef | None:
    schema = getattr(value, "operation_schema", None)
    request = getattr(value, "request", None)
    return (
        request.operation.schema_ref
        if schema is None and isinstance(request, ExecuteRegisteredOperation)
        else schema
    )


def prove_outcome[M: BaseModel](value: M, outcome: BaseModel, schema: OperationSchemaRef) -> M:
    """Attach pure validated codec facts to a newly constructed value."""
    object.__setattr__(
        value,
        "__pydantic_private__",
        {
            **(value.__pydantic_private__ or {}),
            "_outcome_proof": outcome,
            "_schema_proof": schema,
        },
    )
    return value


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
    schema = _schema(value)
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
        if getattr(value, "kind", None) == "operation_result":
            raise OutcomeCodecError("context")
        return value
    decoded = registry.decode_outcome(schema, payload)
    if getattr(value, "outcome_schema", None) not in (None, schema.outcome_schema):
        raise OutcomeCodecError("mismatch")
    return prove_outcome(
        value.model_copy(update={"outcome": decoded, "outcome_json": canonical_json(decoded)}),
        decoded,
        schema,
    )


def _without_registry[M: BaseModel](value: M, payload: str | None, outcome: BaseModel | None) -> M:
    if outcome is not None:
        return value
    if payload is not None or getattr(value, "kind", None) == "operation_result":
        raise OutcomeCodecError("context")
    return value
