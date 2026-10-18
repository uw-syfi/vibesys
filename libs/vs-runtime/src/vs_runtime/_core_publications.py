"""Durable stable-ID publication, using Project-owned atomic namespace writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import ContractError, OperationRegistry
from vs_project.api import DecodeMemo, decode_state_document
from vs_runtime._core_record import (
    PUBLICATION_SCHEMA_VERSION,
    Publication,
    PublicationAcknowledgement,
    PublicationContext,
    PublicationHistory,
    append_publication,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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

    def __init__(
        self,
        namespace: StateNamespace,
        registry: OperationRegistry,
        *,
        on_decode: Callable[[], None] | None = None,
    ) -> None:
        """Bind the journal; ``on_decode`` observes each document decode, which runs once per distinct bytes."""
        self._namespace = namespace
        self._registry = registry
        self._decoded = DecodeMemo(self._decode, on_decode)

    def _decode(self, source: bytes) -> PublicationHistory:
        return decode_state_document(
            PublicationHistory,
            source,
            source="publications.json",
            context={"operation_registry": self._registry},
            versioned_by=("schema_version", PUBLICATION_SCHEMA_VERSION),
        )

    def read(self) -> tuple[Publication, ...]:
        """Read strictly validated durable history, including registered callbacks."""
        source = self._namespace.read_bytes("publications.json")
        if source is None:
            return ()
        return self._decoded.decode(source).publications

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
            contents = journal.model_dump_json().encode()
            self._namespace.write_bytes("publications.json", contents)
            self._decoded.remember(contents, journal)

        if not context.lease.write_if_held(append):
            raise ContractError(("fence",), "publication host no longer owns the run")
        return PublicationAcknowledgement(
            publication_id=publication.publication_id, sequence=publication.sequence
        )
