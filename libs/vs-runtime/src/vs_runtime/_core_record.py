"""Strict atomic shell record, independent of storage CAS and core revisions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vs_core.api import RunEnvelope, StrategyEvent, StrategyState

RUNTIME_SCHEMA_VERSION = 1


class Publication(BaseModel):
    """Stable semantic publication identity, preserved across append/ack crashes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    publication_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    event: StrategyEvent


class RuntimeRecord[S: StrategyState](BaseModel):
    """One StateStore payload; publication acknowledgements do not advance core."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = RUNTIME_SCHEMA_VERSION
    envelope: RunEnvelope[S]
    pending_publications: tuple[Publication, ...] = ()
    delivery_cursor: int = Field(default=0, ge=0)
    next_publication_sequence: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def publication_order(self) -> RuntimeRecord[S]:
        """Reject gaps, duplicates and unacknowledged sequence loss."""
        expected = tuple(range(self.delivery_cursor + 1, self.next_publication_sequence))
        if tuple(row.sequence for row in self.pending_publications) != expected:
            message = "pending_publications: sequences must cover delivery_cursor to next_publication_sequence"
            raise ValueError(message)
        run_id = self.envelope.core.run.run_id.root
        if any(
            row.publication_id != f"{run_id}:{row.sequence}" for row in self.pending_publications
        ):
            message = "pending_publications: publication_id must match run and sequence"
            raise ValueError(message)
        return self
