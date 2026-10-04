"""Durable intent and result receipts that make operation execution idempotent.

An intent receipt is written before an owner performs an effect, and a result
receipt after. Their presence is how a restarted host knows whether an effect
may have started (intent without result) or finished (result). Both are keyed by
request identity and written atomically through a Project-owned namespace.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import ContractError, ObservationStatus, OperationSchemaRef, OperationWire

if TYPE_CHECKING:
    from vs_project.api import StateNamespace

RECEIPT_SCHEMA_VERSION = 1


class IntentReceipt(BaseModel):
    """The effect is about to start. It carries what a later inspection must route on."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = RECEIPT_SCHEMA_VERSION
    request_id: str = Field(min_length=1)
    payload_digest: str = Field(min_length=1)
    operation: OperationWire


class ResultReceipt(BaseModel):
    """The terminal result of one request: replayed verbatim, never recomputed.

    ``outcome_json`` is present exactly when the status is SUCCEEDED.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = RECEIPT_SCHEMA_VERSION
    request_id: str = Field(min_length=1)
    payload_digest: str = Field(min_length=1)
    schema_ref: OperationSchemaRef
    status: ObservationStatus
    outcome_json: str | None = None
    detail: str = ""


class OperationReceipts(Protocol):
    """Durable receipts by request identity."""

    def intent(self, request_id: str) -> IntentReceipt | None: ...

    def record_intent(self, receipt: IntentReceipt) -> None: ...

    def result(self, request_id: str) -> ResultReceipt | None: ...

    def record_result(self, receipt: ResultReceipt) -> None: ...


class NamespaceOperationReceipts:
    """Receipts as one atomically written file each below a Project state namespace.

    Re-recording an identical receipt is a no-op. A different payload under the
    same request identity raises ``ContractError`` naming the request.
    """

    def __init__(self, namespace: StateNamespace) -> None:
        """Bind to the namespace that holds this run's operation receipts."""
        self._namespace = namespace

    @staticmethod
    def _path(request_id: str, part: str) -> str:
        return f"operations/{hashlib.sha256(request_id.encode()).hexdigest()}.{part}.json"

    def intent(self, request_id: str) -> IntentReceipt | None:
        """The recorded intent, or None when the effect never started."""
        return self._namespace.load_optional(self._path(request_id, "intent"), IntentReceipt)

    def record_intent(self, receipt: IntentReceipt) -> None:
        """Durably record the intent, idempotent for the identical payload."""
        prior = self.intent(receipt.request_id)
        if prior == receipt:
            return
        if prior is not None:
            raise ContractError(("request_id",), "same request identity with another payload")
        self._namespace.save(self._path(receipt.request_id, "intent"), receipt)

    def result(self, request_id: str) -> ResultReceipt | None:
        """The recorded terminal result, or None."""
        return self._namespace.load_optional(self._path(request_id, "result"), ResultReceipt)

    def record_result(self, receipt: ResultReceipt) -> None:
        """Durably record the result, idempotent for the identical payload."""
        prior = self.result(receipt.request_id)
        if prior == receipt:
            return
        if prior is not None:
            raise ContractError(("request_id",), "same request identity with another result")
        self._namespace.save(self._path(receipt.request_id, "result"), receipt)
