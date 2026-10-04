"""Durable stable-ID publication, using Project-owned atomic namespace writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import ContractError, OperationRegistry
from vs_runtime._core_record import (
    Publication,
    PublicationAcknowledgement,
    PublicationContext,
    PublicationHistory,
    append_publication,
)

if TYPE_CHECKING:
    from vs_project.api import StateNamespace, StateStore


class JournalPublicationDelivery:
    """Atomic durable journal, with conflict detection on ID and sequence.

    The host's StateStore fence serializes writers. A returned publication is
    durable before acknowledgement; replay after append-before-ack verifies
    exact payload equality and does not append a duplicate. There is no legacy
    journal fallback. Namespace is supplied through Project.portable_namespace.
    """

    def __init__(
        self, namespace: StateNamespace, registry: OperationRegistry, store: StateStore
    ) -> None:
        self._namespace = namespace
        self._registry = registry
        self._store = store

    def read(self) -> tuple[Publication, ...]:
        """Read strictly validated durable history, including registered callbacks."""
        source = self._namespace.read_bytes("publications.json")
        if source is None:
            return ()
        return PublicationHistory.model_validate_json(
            source, context={"operation_registry": self._registry}
        ).publications

    async def publish(
        self, publication: Publication, context: PublicationContext
    ) -> PublicationAcknowledgement:
        """Honor the host fence and durably deduplicate before acknowledgement."""
        if not self._store.verify(context.fence, now=context.now_at):
            raise ContractError(("fence",), "publication host no longer owns the run")
        rows = self.read()
        appended = append_publication(rows, publication)
        if appended == rows:
            return PublicationAcknowledgement(
                publication_id=publication.publication_id, sequence=publication.sequence
            )
        journal = PublicationHistory(schema_version=1, publications=appended)
        self._namespace.write_bytes("publications.json", journal.model_dump_json().encode())
        return PublicationAcknowledgement(
            publication_id=publication.publication_id, sequence=publication.sequence
        )
