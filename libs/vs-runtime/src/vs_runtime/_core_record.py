"""Strict atomic shell record, independent of storage CAS and core revisions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vs_core.api import ContractError, OperationRegistry, RunEnvelope, StrategyEvent, StrategyState
from vs_project.api import StoreFence

if TYPE_CHECKING:
    from vs_project.api import StoredEnvelope

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
    schema_version: Literal[1]
    envelope: RunEnvelope[S]
    pending_publications: tuple[Publication, ...]
    delivery_cursor: int = Field(ge=0)
    next_publication_sequence: int = Field(ge=1)

    @model_validator(mode="after")
    def publication_order(self) -> RuntimeRecord[S]:
        """Reject gaps, duplicates and unacknowledged sequence loss."""
        if self.next_publication_sequence != self.envelope.event_cursor.sequence + 1:
            message = "next_publication_sequence: must follow the envelope event_cursor"
            raise ValueError(message)
        if len(
            self.pending_publications
        ) != self.next_publication_sequence - self.delivery_cursor - 1 or any(
            row.sequence != sequence
            for sequence, row in enumerate(
                self.pending_publications, start=self.delivery_cursor + 1
            )
        ):
            message = "pending_publications: sequences must cover delivery_cursor to next_publication_sequence"
            raise ValueError(message)
        run_id = self.envelope.core.run.run_id.root
        if any(
            row.publication_id != f"{run_id}:{row.sequence}" for row in self.pending_publications
        ):
            message = "pending_publications: publication_id must match run and sequence"
            raise ValueError(message)
        return self

    @classmethod
    def fresh(cls, envelope: RunEnvelope[S]) -> Self:
        """Construct the first complete record, without implicit wire defaults."""
        return cls(
            schema_version=RUNTIME_SCHEMA_VERSION,
            envelope=envelope,
            pending_publications=(),
            delivery_cursor=0,
            next_publication_sequence=1,
        )

    @classmethod
    def decode(cls, stored: StoredEnvelope, registry: OperationRegistry) -> Self:
        """Validate the runtime schema and canonical core codecs before replay."""
        if stored.schema_version != RUNTIME_SCHEMA_VERSION:
            raise ContractError(("runtime", "schema_version"), "unsupported record schema")
        record = cls.model_validate_json(
            stored.payload, context={"operation_registry": registry, "persisted_operation": True}
        )
        envelope = registry.decode_envelope(
            type(record.envelope), record.envelope.model_dump_json()
        )
        return record.model_copy(update={"envelope": envelope})


class PublicationContext(BaseModel):
    """Publication execution epoch, separate from its stable identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    fence: StoreFence
    now_at: float = Field(ge=0, allow_inf_nan=False)


class PublicationAcknowledgement(BaseModel):
    """Positive durable acknowledgement, bound to the exact publication."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    publication_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)


class PublicationHistory(BaseModel):
    """One authoritative strict journal wire contract for all implementations."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1]
    publications: tuple[Publication, ...]

    @model_validator(mode="after")
    def ordered_identities(self) -> Self:
        """Reject duplicate IDs, gaps and mixed-run histories before append."""
        run_id = (
            self.publications[0].publication_id.rsplit(":", 1)[0] if self.publications else None
        )
        for sequence, publication in enumerate(self.publications, start=1):
            if (
                publication.sequence != sequence
                or publication.publication_id != f"{run_id}:{sequence}"
            ):
                message = "publications: contiguous stable identities from one run required"
                raise ValueError(message)
        return self


def append_publication(
    rows: tuple[Publication, ...], publication: Publication
) -> tuple[Publication, ...]:
    """Exact replay is idempotent; conflicting identities or gaps fail loudly."""
    prior = next((row for row in rows if row.publication_id == publication.publication_id), None)
    if prior is not None:
        if prior != publication:
            raise ContractError(("publication_id",), "publication payload conflict")
        return rows
    if publication.sequence != len(rows) + 1:
        raise ContractError(("sequence",), "publication sequence gap or conflict")
    return PublicationHistory(schema_version=1, publications=(*rows, publication)).publications
