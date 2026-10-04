"""Durable intent and result receipts that make operation execution idempotent.

An intent receipt is written before an owner performs an effect, and a result
is sealed after it. Their presence is how a restarted host knows whether an effect
may have started (intent without result) or finished (result). Both are keyed by
request identity and written atomically through the shared ``ReceiptStore``, which
also owns the sealed-result rule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import ObservationStatus, OperationSchemaRef, OperationWire

if TYPE_CHECKING:
    from vs_runtime._receipt_store import ReceiptStore

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

    @property
    def store(self) -> ReceiptStore:
        """The shared store that runs the effect-once rule."""
        ...

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

    def __init__(self, store: ReceiptStore) -> None:
        """Bind to the shared store that holds this run's receipts."""
        self._store = store

    @property
    def store(self) -> ReceiptStore:
        """The shared store, which runs the effect-once rule for these receipts."""
        return self._store

    def intent(self, request_id: str) -> IntentReceipt | None:
        """The recorded intent, or None when the effect never started."""
        return self._store.load(self._FAMILY, "intent", request_id, IntentReceipt)

    def record_intent(self, receipt: IntentReceipt) -> None:
        """Durably record the intent, idempotent for the identical payload."""
        self._store.record_once(self._FAMILY, "intent", receipt.request_id, receipt)

    def result(self, request_id: str) -> ResultReceipt | None:
        """The sealed terminal result, or None."""
        sealed = self._store.sealed(request_id, ResultReceipt)
        return None if sealed is None else sealed[1]

    def record_result(self, receipt: ResultReceipt) -> None:
        """Seal a result an inspection proved, idempotent for the identical result."""
        self._store.seal(receipt.request_id, receipt.payload_digest, receipt)
