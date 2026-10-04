"""Durable evidence ledger: what each evaluation evidence id read as, and why it was taken.

The evaluation executor learns a ``TrustedEvidence`` reading (stage, outcome,
metrics, partial measurement) when a job ends, and core's ``EvidenceRef`` keeps
only identity and artifact digests. This ledger is the missing producer: it
persists each reading with the plan purpose that decides its core evidence kind,
on the shared ``ReceiptStore``, so a later operation (and a restarted host) can
read it back by evidence identity.

An entry is written once. Recording the identical entry again is a no-op, so a
replayed poll is harmless. A different entry for the same identity is a
``ContractError``, so evidence never changes under a reader. Entries are keyed
by the submission request that produced them plus the evidence id, the same
identity core uses (``EvidenceKey``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from vs_core.api import ContractError
from vs_evaluation.api import TrustedEvidence

if TYPE_CHECKING:
    from vs_core.api import EvidenceId, RequestId
    from vs_runtime._receipt_store import ReceiptStore

type MeasurementPurpose = Literal["baseline", "local-validation", "official", "profile"]

_FAMILY = "evidence-ledger"


class EvidenceEntry(BaseModel):
    """One persisted reading and the plan purpose it was measured for."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence: TrustedEvidence
    purpose: MeasurementPurpose


class EvidenceRecorder(Protocol):
    """Write port: the evaluation executor records terminal evidence exactly once."""

    def record(
        self, source_request: RequestId, evidence: TrustedEvidence, purpose: MeasurementPurpose
    ) -> None:
        """Persist the reading. Identical replays pass; a conflicting rewrite raises ContractError."""
        ...


class EvidenceLookup(Protocol):
    """Read port: the reading of one evidence identity, or None when never recorded."""

    def lookup(self, source_request: RequestId, evidence_id: EvidenceId) -> EvidenceEntry | None:
        """The recorded entry. Raises ReceiptCorruptError when a stored entry is unreadable."""
        ...


def _key(source_request: RequestId, evidence_id: str) -> str:
    return f"{source_request.root}\0{evidence_id}"


class ReceiptEvidenceLedger:
    """The ledger on the run's shared ``ReceiptStore``: durable, atomic, cross-process."""

    def __init__(self, store: ReceiptStore) -> None:
        """Bind the store that holds this run's receipts."""
        self._store = store

    def record(
        self, source_request: RequestId, evidence: TrustedEvidence, purpose: MeasurementPurpose
    ) -> None:
        """Persist once; see ``EvidenceRecorder.record``."""
        entry = EvidenceEntry(evidence=evidence, purpose=purpose)
        self._store.record_once(
            _FAMILY, "evidence", _key(source_request, evidence.evidence_id), entry
        )

    def lookup(self, source_request: RequestId, evidence_id: EvidenceId) -> EvidenceEntry | None:
        """Read the entry back from the store."""
        return self._store.load(
            _FAMILY, "evidence", _key(source_request, evidence_id.root), EvidenceEntry
        )


class FakeEvidenceLedger:
    """In-memory ledger with the production recording rule, for tests of readers.

    Tests feed it through ``record``, the same producer port the evaluation
    executor uses, so a reader cannot be tested against a shape the producer
    never writes.
    """

    def __init__(self) -> None:
        """Start empty."""
        self._entries: dict[str, EvidenceEntry] = {}

    def record(
        self, source_request: RequestId, evidence: TrustedEvidence, purpose: MeasurementPurpose
    ) -> None:
        """Same rule as ``ReceiptEvidenceLedger.record``."""
        entry = EvidenceEntry(evidence=evidence, purpose=purpose)
        key = _key(source_request, evidence.evidence_id)
        prior = self._entries.get(key)
        if prior is not None and prior != entry:
            raise ContractError(("evidence_id",), "same evidence identity with another payload")
        self._entries[key] = entry

    def lookup(self, source_request: RequestId, evidence_id: EvidenceId) -> EvidenceEntry | None:
        """The entry recorded for this identity, or None."""
        return self._entries.get(_key(source_request, evidence_id.root))
