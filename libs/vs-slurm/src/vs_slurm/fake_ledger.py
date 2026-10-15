"""An in-memory evaluation ledger with the same rules as the one on the cluster."""

from __future__ import annotations

from threading import RLock

from .ledger import (
    LedgerEntry,
    LedgerError,
    LedgerLease,
    LedgerRecord,
    decode_lease,
    decode_record,
    encode,
    validate_holder,
    validate_key,
    validate_project,
)


class _Store:
    """The documents of every project, shared by every reopened Fake, as a filesystem would be."""

    def __init__(self) -> None:
        self.lock = RLock()
        # (project, key) -> attempt -> encoded record; (project, key) -> holder -> encoded lease
        self.records: dict[tuple[str, str], dict[int, str]] = {}
        self.leases: dict[tuple[str, str], dict[str, str]] = {}


class FakeEvaluationLedger:
    """Faithful in-memory ledger: documents are stored encoded and decoded on read.

    ``reopen`` gives another process's view of the same storage; ``store_raw``
    plants a document as an older build would have written it.
    """

    def __init__(self, *, project: str, _store: _Store | None = None) -> None:
        """Create an empty ledger for ``project``."""
        self._project = validate_project(project)
        self._store = _store if _store is not None else _Store()

    def reopen(self, *, project: str | None = None) -> FakeEvaluationLedger:
        """Another process's ledger over the same storage."""
        return FakeEvaluationLedger(project=project or self._project, _store=self._store)

    def store_raw(self, key: str, attempt: int, document: str) -> None:
        """Plant an attempt's stored bytes, for format compatibility tests."""
        with self._store.lock:
            self._store.records.setdefault((self._project, validate_key(key)), {})[attempt] = (
                document
            )

    def read(self, key: str) -> LedgerEntry | None:
        """The request's current attempt and its leases."""
        slot = (self._project, validate_key(key))
        with self._store.lock:
            attempts = dict(self._store.records.get(slot, {}))
            leases = dict(self._store.leases.get(slot, {}))
        if not attempts:
            return None
        records = [
            decode_record(text, path=f"{key}/attempt-{attempt}.json")
            for attempt, text in attempts.items()
        ]
        for record in records:
            if record.key != key:
                raise LedgerError.unreadable(key, "record names another key")
        current = max(records, key=lambda record: record.attempt)
        decoded = tuple(
            decode_lease(text, path=f"{key}/leases/{holder}.json")
            for holder, text in sorted(leases.items())
        )
        return LedgerEntry(current, decoded)

    def claim(self, record: LedgerRecord) -> LedgerRecord:
        """Create the record's attempt unless one exists; return the stored attempt."""
        slot = (self._project, validate_key(record.key))
        with self._store.lock:
            attempts = self._store.records.setdefault(slot, {})
            if record.attempt not in attempts:
                attempts[record.attempt] = encode(record)
                return record
        entry = self.read(record.key)
        if entry is None:
            raise LedgerError.claim_unreadable(record.key)
        return entry.record

    def update(self, record: LedgerRecord) -> None:
        """Replace the state of an attempt."""
        slot = (self._project, validate_key(record.key))
        with self._store.lock:
            self._store.records.setdefault(slot, {})[record.attempt] = encode(record)

    def renew(self, key: str, lease: LedgerLease) -> None:
        """Write this holder's lease."""
        slot = (self._project, validate_key(key))
        with self._store.lock:
            self._store.leases.setdefault(slot, {})[validate_holder(lease.holder)] = encode(lease)

    def release(self, key: str, holder: str) -> None:
        """Drop this holder's lease."""
        slot = (self._project, validate_key(key))
        with self._store.lock:
            self._store.leases.get(slot, {}).pop(validate_holder(holder), None)

    def list_keys(self) -> tuple[str, ...]:
        """Every request the project's ledger holds."""
        with self._store.lock:
            return tuple(
                sorted(key for project, key in self._store.records if project == self._project)
            )


__all__ = ["FakeEvaluationLedger"]
