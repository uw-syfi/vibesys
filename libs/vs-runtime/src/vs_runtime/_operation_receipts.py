"""Durable intent and result receipts that make operation execution idempotent.

An intent receipt is written before an owner performs an effect, and a result
receipt after. Their presence is how a restarted host knows whether an effect
may have started (intent without result) or finished (result). Both are keyed by
request identity and written atomically through a Project-owned namespace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import ObservationStatus, OperationSchemaRef, OperationWire
from vs_runtime._receipt_store import ReceiptStore

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

    _FAMILY = "operations"

    def __init__(self, namespace: StateNamespace) -> None:
        """Bind to the namespace that holds this run's operation receipts."""
        self._store = ReceiptStore(namespace)

    def intent(self, request_id: str) -> IntentReceipt | None:
        """The recorded intent, or None when the effect never started."""
        return self._store.load(self._FAMILY, "intent", request_id, IntentReceipt)

    def record_intent(self, receipt: IntentReceipt) -> None:
        """Durably record the intent, idempotent for the identical payload."""
        self._store.record_once(self._FAMILY, "intent", receipt.request_id, receipt)

    def result(self, request_id: str) -> ResultReceipt | None:
        """The recorded terminal result, or None."""
        return self._store.load(self._FAMILY, "result", request_id, ResultReceipt)

    def record_result(self, receipt: ResultReceipt) -> None:
        """Durably record the result, idempotent for the identical payload."""
        self._store.record_once(self._FAMILY, "result", receipt.request_id, receipt)
