"""Durable stable-ID publication, using Project-owned atomic namespace writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import ContractError, OperationRegistry
from vs_project.api import decode_state_document
from vs_runtime._core_record import (
    PUBLICATION_SCHEMA_VERSION,
    Publication,
    PublicationAcknowledgement,
    PublicationContext,
    PublicationHistory,
    append_publication,
)

if TYPE_CHECKING:
    from vs_project.api import StateNamespace


class JournalPublicationDelivery:
    """Atomic durable journal, with conflict detection on ID and sequence.

    The host's fence check and the journal mutation share one store transaction,
    so takeover cannot interleave between ownership validation and replacement.
    A returned publication is durable before acknowledgement; replay after
    append-before-ack verifies exact payload equality and does not append a
    duplicate. There is no legacy journal fallback. Namespace is supplied
    through Project.portable_namespace.
    """

    def __init__(self, namespace: StateNamespace, registry: OperationRegistry) -> None:
        self._namespace = namespace
        self._registry = registry

    def read(self) -> tuple[Publication, ...]:
        """Read strictly validated durable history, including registered callbacks."""
        source = self._namespace.read_bytes("publications.json")
        if source is None:
            return ()
        return decode_state_document(
            PublicationHistory,
            source,
            source="publications.json",
            context={"operation_registry": self._registry},
            versioned_by=("schema_version", PUBLICATION_SCHEMA_VERSION),
        ).publications

    async def publish(
        self, publication: Publication, context: PublicationContext
    ) -> PublicationAcknowledgement:
        """Honor the host fence and durably deduplicate before acknowledgement."""

        def append() -> None:
            rows = self.read()
            appended = append_publication(rows, publication)
            if appended == rows:
                return
            journal = PublicationHistory(
                schema_version=PUBLICATION_SCHEMA_VERSION, publications=appended
            )
            self._namespace.write_bytes("publications.json", journal.model_dump_json().encode())

        if not context.lease.write_if_held(append):
            raise ContractError(("fence",), "publication host no longer owns the run")
        return PublicationAcknowledgement(
            publication_id=publication.publication_id, sequence=publication.sequence
        )
