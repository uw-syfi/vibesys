"""An in-memory evaluation ledger with the same rules as the one on the cluster."""

from __future__ import annotations

import hashlib

from .ledger import (
    LedgerEntry,
    LedgerError,
    LedgerLease,
    LedgerRecord,
    UnreadableLease,
    check_transition,
    decode_lease,
    decode_record,
    encode,
    validate_holder,
    validate_key,
    validate_project,
)


class _Store:
    """The documents of every project, shared by every reopened Fake, as a filesystem would be.

    Every change is one indivisible dict operation (``setdefault``, an item
    assignment or ``pop``), so callers on several worker threads need no lock,
    as the cluster's filesystem needs none: a record revision is created at most
    once, and a reader copies a whole mapping.
    """

    def __init__(self) -> None:
        # (project, key) -> (attempt, revision) -> encoded record
        self.records: dict[tuple[str, str], dict[tuple[int, int], str]] = {}
        # (project, key) -> holder -> (encoded lease, when its file was last written)
        self.leases: dict[tuple[str, str], dict[str, tuple[str, float]]] = {}
        # The cluster's filesystem outside the ledger: absolute path -> bytes.
        self.operator_files: dict[str, bytes] = {}


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

    def store_raw(self, key: str, attempt: int, document: str, revision: int = 0) -> None:
        """Plant a record revision's stored bytes, for format compatibility tests."""
        slot = (self._project, validate_key(key))
        self._store.records.setdefault(slot, {})[(attempt, revision)] = document

    def store_raw_lease(self, key: str, holder: str, document: str, *, written_at: float) -> None:
        """Plant a lease's stored bytes, as another build would have written them at ``written_at``."""
        slot = (self._project, validate_key(key))
        self._store.leases.setdefault(slot, {})[holder] = (document, written_at)

    def read(self, key: str) -> LedgerEntry | None:
        """The request's current attempt, the revision it was read at, and its leases."""
        slot = (self._project, validate_key(key))
        revisions = dict(self._store.records.get(slot, {}))
        leases = dict(self._store.leases.get(slot, {}))
        if not revisions:
            return None
        decoded: list[tuple[tuple[int, int], LedgerRecord]] = []
        for (attempt, revision), text in revisions.items():
            path = f"{key}/attempt-{attempt}.{revision}.json"
            record = decode_record(text, path=path)
            if record.key != key or record.attempt != attempt:
                raise LedgerError.unreadable(path, "the record names another key or attempt")
            decoded.append(((attempt, revision), record))
        latest: dict[int, tuple[int, LedgerRecord]] = {}
        for (attempt, revision), record in decoded:
            if attempt not in latest or revision > latest[attempt][0]:
                latest[attempt] = (revision, record)
        *earlier, (revision, current) = (latest[attempt] for attempt in sorted(latest))
        readable: list[LedgerLease] = []
        unreadable: list[UnreadableLease] = []
        for holder, (text, written_at) in sorted(leases.items()):
            try:
                readable.append(decode_lease(text, path=f"{key}/leases/{holder}.json"))
            except LedgerError:
                unreadable.append(UnreadableLease(holder, written_at))
        return LedgerEntry(
            current,
            tuple(readable),
            revision,
            tuple(unreadable),
            tuple(record for _, record in earlier),
        )

    def claim(self, record: LedgerRecord) -> LedgerRecord:
        """Create the record's attempt unless one exists; return the stored attempt."""
        if self._create(record, 0):
            return record
        entry = self.read(record.key)
        if entry is None or entry.record.attempt < record.attempt:
            raise LedgerError.claim_unreadable(record.key)
        return entry.record

    def replace(self, current: LedgerEntry, record: LedgerRecord) -> bool:
        """Write ``record`` as the next revision of ``current``; False if another writer won."""
        check_transition(current.record, record)
        return self._create(record, current.revision + 1)

    def renew(self, key: str, lease: LedgerLease) -> None:
        """Write this holder's lease; its file is written when the lease was renewed."""
        slot = (self._project, validate_key(key))
        holder = validate_holder(lease.holder)
        self._store.leases.setdefault(slot, {})[holder] = (encode(lease), lease.renewed_at)

    def release(self, key: str, holder: str) -> None:
        """Drop this holder's lease."""
        slot = (self._project, validate_key(key))
        self._store.leases.get(slot, {}).pop(validate_holder(holder), None)

    def put_operator_file(self, path: str, content: bytes) -> None:
        """Place a file on the cluster's filesystem, as an operator would (a setup script)."""
        self._store.operator_files[path] = content

    def operator_file_sha256(self, path: str) -> str | None:
        """The SHA-256 of a file on the cluster's filesystem, or None when there is no such file."""
        content = self._store.operator_files.get(path)
        return None if content is None else hashlib.sha256(content).hexdigest()

    def list_keys(self) -> tuple[str, ...]:
        """Every request the project's ledger holds."""
        return tuple(
            sorted(key for project, key in self._store.records if project == self._project)
        )

    def _create(self, record: LedgerRecord, revision: int) -> bool:
        """Store a record revision unless it exists, as a create-only link does."""
        revisions = self._store.records.setdefault((self._project, validate_key(record.key)), {})
        encoded = encode(record)
        # One dict.setdefault is one indivisible step, as one link(2) is: of two
        # writers of a revision, from any thread, exactly one stores its document.
        return revisions.setdefault((record.attempt, revision), encoded) is encoded


__all__ = ["FakeEvaluationLedger"]
