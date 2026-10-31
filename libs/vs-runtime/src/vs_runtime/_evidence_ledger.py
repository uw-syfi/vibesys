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
by core's ``EvidenceKey``: the submission request that produced them plus the
evidence id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from vs_core.api import ContractError, EvidenceId, EvidenceKey
from vs_evaluation.api import TrustedEvidence

if TYPE_CHECKING:
    from vs_core.api import MeasurementPlan, RequestId
    from vs_evaluation.api import ExecutorPoll
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

    def lookup(self, key: EvidenceKey) -> EvidenceEntry | None:
        """The recorded entry. Raises ReceiptCorruptError when a stored entry is unreadable."""
        ...


class EvidenceLedger(EvidenceRecorder, EvidenceLookup, Protocol):
    """Both ports: a producer that settles a reading once reads back what it or an earlier try wrote."""


def ledgered_poll(
    ledger: EvidenceLedger,
    polled: ExecutorPoll,
    *,
    source_request: RequestId,
    plan: MeasurementPlan,
) -> ExecutorPoll:
    """``polled`` with each stage reading recorded once; a later try that reads differently gets the stored one.

    Evidence never changes under a reader, so a replay returns the stored entry instead of failing
    inside the pure translation. Whoever reports a job's end to core calls this first: the operation
    that decodes the evidence reads only this ledger (``InterpretEvidenceOwner``).
    """
    terminal = polled.terminal
    if terminal is None:
        return polled
    known = {stage.stage_id for stage in plan.stages}
    steps = []
    for step in terminal.stage_results:
        if step.result is None or step.name not in known:
            steps.append(step)
            continue
        item = TrustedEvidence.model_validate(step.result)
        key = EvidenceKey(
            source_request=source_request, evidence_id=EvidenceId(root=item.evidence_id)
        )
        stored = ledger.lookup(key)
        if stored is None:
            ledger.record(source_request, item, plan.purpose)
            steps.append(step)
        else:
            steps.append(
                step.model_copy(update={"result": stored.evidence.model_dump(mode="json")})
            )
    return polled.model_copy(
        update={"terminal": terminal.model_copy(update={"stage_results": tuple(steps)})}
    )


def _key(key: EvidenceKey) -> str:
    return f"{key.source_request.root}\0{key.evidence_id.root}"


def _key_of(source_request: RequestId, evidence: TrustedEvidence) -> EvidenceKey:
    return EvidenceKey(
        source_request=source_request, evidence_id=EvidenceId(root=evidence.evidence_id)
    )


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
        self._store.record_once(_FAMILY, "evidence", _key(_key_of(source_request, evidence)), entry)

    def lookup(self, key: EvidenceKey) -> EvidenceEntry | None:
        """Read the entry back from the store."""
        return self._store.load(_FAMILY, "evidence", _key(key), EvidenceEntry)


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
        key = _key(_key_of(source_request, evidence))
        prior = self._entries.get(key)
        if prior is not None and prior != entry:
            raise ContractError(("evidence_id",), "same evidence identity with another payload")
        self._entries[key] = entry

    def lookup(self, key: EvidenceKey) -> EvidenceEntry | None:
        """The entry recorded for this identity, or None."""
        return self._entries.get(_key(key))
