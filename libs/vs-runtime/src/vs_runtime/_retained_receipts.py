"""Schemas of receipt families the step loop no longer writes, kept for the retained-run import.

A run written before the step loop left a record and a sealed result for every registered
operation, and a published diagnostic for every blocked request. Nothing writes them now. The
import of such a run (``vs_runtime.api.retained``) still decodes them, so their strict
schemas stay here, read-only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import ObservationStatus, OperationSchemaRef, OperationWire

RECEIPT_SCHEMA_VERSION = 1


class RecordReceipt(BaseModel):
    """The effect was about to start. It carries what a later inspection must route on."""

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


class Published(BaseModel):
    """The sealed result of one blocked request: the journal position its diagnostic held."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    sequence: int = Field(ge=1)
