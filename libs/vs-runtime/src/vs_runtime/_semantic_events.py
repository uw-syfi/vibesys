"""Committed blocked-intent diagnostics, deduplicated by request identity.

``BlockIntent`` reports that a target request is blocked and says why. The core
publication journal carries only strategy events and has no diagnostic variant,
so these diagnostics go to a runtime-owned journal in the same Project namespace.
Delivery is an exact replay: the same request identity with the same payload is
acknowledged without a second row, and a different payload is a conflict.
The acknowledgement covers the command only. The target stays owned and blocked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import (
    BlockIntent,
    ContractError,
    EventId,
    Observation,
    ObservationStatus,
    RequestObserved,
    Scope,
)
from vs_runtime._core_requests import ExecutionContext, ExecutionResult

if TYPE_CHECKING:
    from vs_project.api import StateNamespace

_JOURNAL = "block-diagnostics.json"


class BlockDiagnostic(BaseModel):
    """One published diagnostic. The sequence is its position in the journal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    sequence: int = Field(ge=1)
    request_id: str = Field(min_length=1)
    target: str = Field(min_length=1)
    scope: Scope
    diagnostic: str


class BlockDiagnostics(BaseModel):
    """The journal file: contiguous sequences, one row per request identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    rows: tuple[BlockDiagnostic, ...] = ()


class JournalSemanticEvents:
    """The SEMANTIC_EVENTS role over a Project state namespace."""

    def __init__(self, namespace: StateNamespace) -> None:
        """Bind the namespace that holds this run's diagnostics journal."""
        self._namespace = namespace

    def read(self) -> tuple[BlockDiagnostic, ...]:
        """Every published diagnostic in order."""
        journal = self._namespace.load_optional(_JOURNAL, BlockDiagnostics)
        return () if journal is None else journal.rows

    async def execute(self, request: BlockIntent, context: ExecutionContext) -> ExecutionResult:
        """Publish once per request identity, then acknowledge that command only."""
        if request.request_id is None:
            raise ContractError(("request_id",), "canonical identity required")
        rows = self.read()
        row = BlockDiagnostic(
            sequence=len(rows) + 1,
            request_id=request.request_id.root,
            target=request.target.root,
            scope=request.scope,
            diagnostic=request.diagnostic,
        )
        prior = next((item for item in rows if item.request_id == row.request_id), None)
        if prior is None:
            self._namespace.save(_JOURNAL, BlockDiagnostics(rows=(*rows, row)))
        elif prior.model_copy(update={"sequence": row.sequence}) != row:
            raise ContractError(("request_id",), "same request identity with another diagnostic")
        return ExecutionResult(
            observation=RequestObserved(
                observation=Observation(
                    event_id=EventId(root=f"{request.request_id.root}:observation:0"),
                    request_id=request.request_id,
                    scope=request.scope,
                    admission_id=request.admission_id,
                    sequence=0,
                    observed_at=context.now_at,
                    status=ObservationStatus.SUCCEEDED,
                    accepted=True,
                    terminal=True,
                )
            )
        )
