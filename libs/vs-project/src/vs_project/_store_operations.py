"""Shared state-store transition semantics; implementations own I/O only.

A store keeps two documents under one lock. ``store.json`` holds the CAS record, the whole
run envelope, and is written only by a commit. ``lease.json`` holds the fence and the time
watermark, a few hundred bytes, and is written by every lease operation and after every
commit. Renewing a lease once rewrote the whole record, so its cost grew with the run
(FINDINGS S171); now it writes the small document only.

Version 1 of ``store.json`` held the lease too, and had no ``lease.json``. Older releases
read only ``store.json``, and only version 1, so while ``store.json`` is version 1 (or absent)
an older host may take, renew or release the lease there at any moment. This release
therefore grants nothing until ``store.json`` is version 2, which older releases refuse:

1. it copies the lease it read into ``lease.json``, unchanged and marked not ``migrated``;
2. it rewrites ``store.json`` as version 2, holding the same record (an older release now
   refuses the store);
3. only then does it write the lease it grants, marked ``migrated``.

``migrated`` is set only once ``store.json`` version 2 is durable, so a reader that finds it
trusts ``lease.json`` alone and never reads the record for a lease operation. Until then a
reader reads both: a version 2 ``store.json`` makes ``lease.json`` the lease; a version 1 or
absent one makes the lease the later of the two (by epoch, then expiry), because an older host
may have advanced the copy in ``store.json`` since step 1. A crash at any step leaves a store
that either release reads consistently, and the next write of this release finishes the
migration. A version 2 ``store.json`` without a ``lease.json`` is damage: step 1 precedes it,
and inventing an empty lease would let the next host reuse an epoch.

After step 2 an older release refuses the store ("document version 2, this release reads
version 1"): resuming a run once on this release ends rollback for that run.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vs_project.api.state_store import (
    CommitFault,
    CommitOutcome,
    Committed,
    Conflict,
    ConflictReason,
    ObservationFault,
    QuarantinedEnvelope,
    StateStoreWriteError,
    StoredEnvelope,
    StoreFence,
    StoreRecord,
    Unknown,
)
from vs_project.errors import StateDocumentDamagedError

LEGACY_STORE_DOCUMENT_VERSION = 1
STORE_DOCUMENT_VERSION = 2
LEASE_DOCUMENT_VERSION = 1


class StoreFile(StrEnum):
    """The two documents of one store, by file name."""

    RECORD = "store.json"
    LEASE = "lease.json"


_CONFIG = ConfigDict(
    extra="forbid", frozen=True, strict=True, ser_json_bytes="base64", val_json_bytes="base64"
)


class StoreDocument(BaseModel):
    """``store.json``: the CAS record; version 1, from older releases, also held the lease."""

    model_config = _CONFIG
    # A document that declares no version is the oldest format, as older releases wrote it.
    version: int = Field(
        default=LEGACY_STORE_DOCUMENT_VERSION,
        ge=LEGACY_STORE_DOCUMENT_VERSION,
        le=STORE_DOCUMENT_VERSION,
    )
    record: StoreRecord | None = None
    fence: StoreFence | None = None
    observed_at: float = Field(default=0, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _lease_only_in_the_legacy_format(self) -> Self:
        if self.version != LEGACY_STORE_DOCUMENT_VERSION and (
            self.fence is not None or self.observed_at != 0
        ):
            message = f"store.json version {self.version} holds no lease; it is in lease.json"
            raise ValueError(message)
        return self

    @property
    def legacy(self) -> bool:
        """Whether an older release wrote this document, with the lease inside it."""
        return self.version == LEGACY_STORE_DOCUMENT_VERSION

    def encode(self) -> bytes:
        """The document's bytes, leaving out the lease fields the current format never has."""
        exclude = None if self.legacy else {"fence", "observed_at"}
        return self.model_dump_json(exclude=exclude).encode()


def record_document(record: StoreRecord | None) -> StoreDocument:
    """The current-format ``store.json`` holding ``record``."""
    return StoreDocument(version=STORE_DOCUMENT_VERSION, record=record)


class LeaseDocument(BaseModel):
    """``lease.json``: the dispatch fence and the time watermark of the last published write."""

    model_config = _CONFIG
    version: int = Field(
        default=LEASE_DOCUMENT_VERSION, ge=LEASE_DOCUMENT_VERSION, le=LEASE_DOCUMENT_VERSION
    )
    fence: StoreFence | None = None
    observed_at: float = Field(default=0, ge=0, allow_inf_nan=False)
    # Set only once store.json version 2 is durable: from then on older releases refuse the
    # store, and this document alone is the lease (see the module).
    migrated: bool = False

    def encode(self) -> bytes:
        """The document's bytes."""
        return self.model_dump_json().encode()


def _migrated(lease: LeaseDocument) -> LeaseDocument:
    """``lease`` marked as written after ``store.json`` version 2 is durable."""
    return lease if lease.migrated else lease.model_copy(update={"migrated": True})


class _Time(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    now: float = Field(ge=0, allow_inf_nan=False)
    duration: float = Field(default=1, gt=0, allow_inf_nan=False)


class _Expected(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision: int | None = Field(ge=0)


def _valid_fence(lease: LeaseDocument, fence: StoreFence, now: float) -> bool:
    current = lease.fence
    return (
        current is not None
        and current.host_id == fence.host_id
        and current.epoch == fence.epoch
        and lease.observed_at <= now < current.expires_at
    )


def _later(stored: LeaseDocument, moved: LeaseDocument | None) -> LeaseDocument:
    """The lease of a store not yet migrated: the later of ``store.json``'s and ``lease.json``'s.

    An older host may have taken or renewed the lease in ``store.json`` after this release
    copied it to ``lease.json``, so neither copy alone is the lease. Epochs only grow, and a
    renewal only extends expiry, so the later fence is the current one; the watermark is the
    later of the two, as each only ever advances.
    """
    if moved is None:
        return stored
    later = max(
        (moved, stored),
        key=lambda lease: (
            (-1, 0.0) if lease.fence is None else (lease.fence.epoch, lease.fence.expires_at)
        ),
    )
    return LeaseDocument(fence=later.fence, observed_at=max(stored.observed_at, moved.observed_at))


@dataclass(frozen=True)
class _Lease:
    """The lease an operation read, and what it read of ``store.json`` to find it.

    ``record`` is ``store.json`` as read while the store is not ``migrated``, None when there is
    none; a migrated store's lease operations do not read it.
    """

    document: LeaseDocument
    record: StoreDocument | None = None

    @property
    def migrated(self) -> bool:
        """Whether ``store.json`` is known to be version 2, which older releases refuse."""
        return self.document.migrated or (self.record is not None and not self.record.legacy)


class StoreOperations(ABC):
    """Apply the same validated transitions to memory or serialized storage."""

    def __init__(
        self,
        fault_plan: Iterable[CommitFault],
        lease_fault_plan: Iterable[CommitFault | None] = (),
        observation_fault_plan: Iterable[ObservationFault | None] = (),
    ) -> None:
        self._faults = iter(tuple(CommitFault(fault) for fault in fault_plan))
        self._lease_faults = iter(
            tuple(None if fault is None else CommitFault(fault) for fault in lease_fault_plan)
        )
        self._observation_faults = iter(
            tuple(
                None if fault is None else ObservationFault(fault)
                for fault in observation_fault_plan
            )
        )
        self._pending_observation: ObservationFault | None = None

    @abstractmethod
    def _transaction(self) -> AbstractContextManager[None]: ...

    @abstractmethod
    def _read_record(self) -> StoreDocument | None:
        """The decoded ``store.json``, or None when there is none; see ``_observation``."""

    @abstractmethod
    def _read_lease(self) -> LeaseDocument | None:
        """The decoded ``lease.json``, or None when there is none; see ``_observation``."""

    @abstractmethod
    def _write(self, file: StoreFile, document: StoreDocument | LeaseDocument) -> None: ...

    @contextmanager
    def _operation(self) -> Iterator[None]:
        """One store operation: the lock, and this operation's place in the observation plan."""
        with self._transaction():
            self._pending_observation = next(self._observation_faults, None)
            try:
                yield
            finally:
                self._pending_observation = None

    def _observation(self, *, exists: bool) -> ObservationFault | None:
        """The observation fault a read applies: READ at the first read, SYNC at the first found file.

        Each operation takes one entry of the observation plan, as when one document held
        everything, and the read that would have failed then fails now.
        """
        fault = self._pending_observation
        if fault == ObservationFault.READ or (fault is not None and exists):
            self._pending_observation = None
            return fault
        return None

    def _lease(self) -> _Lease:
        lease = self._read_lease()
        if lease is not None and lease.migrated:
            return _Lease(document=lease)
        stored = self._read_record()
        if stored is not None and not stored.legacy:
            if lease is None:
                message = "store.json is in the current format but lease.json is missing"
                raise StateDocumentDamagedError.invalid(StoreFile.LEASE, message)
            return _Lease(document=lease, record=stored)
        legacy = (
            LeaseDocument()
            if stored is None
            else LeaseDocument(fence=stored.fence, observed_at=stored.observed_at)
        )
        return _Lease(document=_later(legacy, lease), record=stored)

    def load(self) -> StoreRecord | None:
        """Read the current entire record under the store lock."""
        with self._operation():
            stored = self._read_record()
            return None if stored is None else stored.record

    def acquire(self, host_id: str, now: float, duration: float) -> StoreFence | None:
        """Claim only absent or expired ownership, increasing the epoch."""
        timing = _Time(now=now, duration=duration)
        candidate = StoreFence(host_id=host_id, epoch=1, expires_at=timing.now + timing.duration)
        with self._operation():
            lease = self._lease()
            current = lease.document.fence
            if timing.now < lease.document.observed_at or (
                current is not None and timing.now < current.expires_at
            ):
                return None
            fence = candidate.model_copy(
                update={"epoch": 1 if current is None else current.epoch + 1}
            )
            self._write_lease(lease, LeaseDocument(fence=fence, observed_at=timing.now))
            return fence

    def renew(self, fence: StoreFence, now: float, duration: float) -> StoreFence | None:
        """Extend matching ownership, preserving the authoritative lease."""
        timing = _Time(now=now, duration=duration)
        with self._operation():
            lease = self._lease()
            current = lease.document.fence
            if current is None or not _valid_fence(lease.document, fence, timing.now):
                return None
            renewed = StoreFence(
                host_id=current.host_id,
                epoch=current.epoch,
                expires_at=max(current.expires_at, timing.now + timing.duration),
            )
            self._write_lease(lease, LeaseDocument(fence=renewed, observed_at=timing.now))
            return renewed

    def release(self, fence: StoreFence, now: float) -> bool:
        """End matching ownership at ``now`` so the next host can acquire at once."""
        timing = _Time(now=now)
        with self._operation():
            lease = self._lease()
            current = lease.document.fence
            if current is None or not _valid_fence(lease.document, fence, timing.now):
                return False
            released = StoreFence(
                host_id=current.host_id, epoch=current.epoch, expires_at=timing.now
            )
            self._write_lease(lease, LeaseDocument(fence=released, observed_at=timing.now))
            return True

    def verify(self, fence: StoreFence, now: float) -> bool:
        """Check persisted owner, epoch, expiry and time watermark."""
        timing = _Time(now=now)
        with self._operation():
            return _valid_fence(self._lease().document, fence, timing.now)

    def mutate_if_fenced(self, fence: StoreFence, now: float, mutation: Callable[[], None]) -> bool:
        """Run a synchronous external mutation under the lease transaction."""
        timing = _Time(now=now)
        with self._operation():
            if not _valid_fence(self._lease().document, fence, timing.now):
                return False
            mutation()
            return True

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome:
        """CAS a runnable opaque record under current host ownership."""
        return self._commit(expected_revision, envelope, fence, now)

    def quarantine(
        self,
        expected_revision: int | None,
        envelope: QuarantinedEnvelope,
        fence: StoreFence,
        now: float,
    ) -> CommitOutcome:
        """CAS an explicit non-runnable migration record."""
        return self._commit(expected_revision, envelope, fence, now)

    def _commit(
        self, expected_revision: int | None, envelope: StoreRecord, fence: StoreFence, now: float
    ) -> CommitOutcome:
        expected = _Expected(revision=expected_revision).revision
        timing = _Time(now=now)
        next_revision = 0 if expected is None else expected + 1
        if envelope.revision != next_revision:
            message = f"revision must be {next_revision}, got {envelope.revision}"
            raise ValueError(message)
        with self._operation():
            lease = self._lease()
            stored = self._read_record() if lease.document.migrated else lease.record
            current = None if stored is None else stored.record
            revision = None if current is None else current.revision
            if revision != expected:
                return Conflict(reason=ConflictReason.REVISION, revision=revision)
            if not _valid_fence(lease.document, fence, timing.now):
                return Conflict(reason=ConflictReason.FENCE, revision=revision)
            self._migrate(lease)
            return self._publish(_migrated(lease.document), envelope, timing.now)

    def _publish(self, lease: LeaseDocument, envelope: StoreRecord, now: float) -> CommitOutcome:
        fault = next(self._faults, None)
        if fault == CommitFault.FAILED:
            message = "state-store fault plan rejected publication before writing"
            raise StateStoreWriteError(message)
        try:
            self._write_document(StoreFile.RECORD, record_document(envelope), fault)
        except OSError:
            self._advance_if_published(lease, envelope, now)
            return Unknown(revision=envelope.revision)
        # The record is published, so the outcome is Committed whatever happens next. The
        # watermark that follows it is its own small write; when that fails, the watermark
        # stays at the previous write's time until the next lease write, which only leaves the
        # clock check as loose as it was before this commit.
        with suppress(OSError):
            self._write_document(
                StoreFile.LEASE, lease.model_copy(update={"observed_at": now}), None
            )
        return Committed(record=envelope)

    def _advance_if_published(
        self, lease: LeaseDocument, envelope: StoreRecord, now: float
    ) -> None:
        """Move the watermark to ``now`` when the unacknowledged record write did happen.

        Only a published write advances the watermark. Whether this one was published is
        what the record now holds; when that cannot be read either, the watermark stays,
        which only leaves the time check as loose as it was before the commit.
        """
        try:
            stored = self._read_record()
            if stored is not None and stored.record == envelope:
                self._write_document(
                    StoreFile.LEASE, lease.model_copy(update={"observed_at": now}), None
                )
        except OSError:
            return

    def _write_lease(self, lease: _Lease, document: LeaseDocument) -> None:
        self._migrate(lease)
        fault = next(self._lease_faults, None)
        self._write_document(StoreFile.LEASE, _migrated(document), fault)

    def _migrate(self, lease: _Lease) -> None:
        """Make ``store.json`` version 2 before anything is granted (see the module).

        Copy the lease read into ``lease.json``, then rewrite ``store.json`` as version 2. A
        failure of either write fails the operation, which has granted nothing yet.
        """
        if lease.migrated:
            return
        self._write_document(StoreFile.LEASE, lease.document, None)
        record = None if lease.record is None else lease.record.record
        self._write_document(StoreFile.RECORD, record_document(record), None)

    def _write_document(
        self, file: StoreFile, document: StoreDocument | LeaseDocument, fault: CommitFault | None
    ) -> None:
        if fault == CommitFault.FAILED:
            message = "state-store lease fault rejected publication before writing"
            raise StateStoreWriteError(message)
        if fault == CommitFault.UNKNOWN_BEFORE:
            message = "state-store acknowledgement lost before publication"
            raise OSError(message)
        self._write(file, document)
        if fault in (CommitFault.UNKNOWN_AFTER, CommitFault.UNKNOWN_SYNC):
            message = "state-store acknowledgement lost after publication"
            raise OSError(message)
