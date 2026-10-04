"""Shared state-store transition semantics; implementations own I/O only."""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import AbstractContextManager

from pydantic import BaseModel, ConfigDict, Field

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


class StoreDocument(BaseModel):
    """One atomic document owns both CAS record and dispatch fencing."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, ser_json_bytes="base64", val_json_bytes="base64"
    )
    version: int = Field(default=1, ge=1, le=1)
    record: StoreRecord | None = None
    fence: StoreFence | None = None
    observed_at: float = Field(default=0, ge=0, allow_inf_nan=False)


class _Time(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    now: float = Field(ge=0, allow_inf_nan=False)
    duration: float = Field(default=1, gt=0, allow_inf_nan=False)


class _Expected(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision: int | None = Field(ge=0)


def _valid_fence(document: StoreDocument, fence: StoreFence, now: float) -> bool:
    current = document.fence
    return (
        current is not None
        and current.host_id == fence.host_id
        and current.epoch == fence.epoch
        and document.observed_at <= now < current.expires_at
    )


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

    @abstractmethod
    def _transaction(self) -> AbstractContextManager[None]: ...

    @abstractmethod
    def _read(self) -> StoreDocument: ...

    @abstractmethod
    def _write(self, document: StoreDocument) -> None: ...

    def load(self) -> StoreRecord | None:
        """Read the current entire record under the store lock."""
        with self._transaction():
            return self._read().record

    def acquire(self, host_id: str, now: float, duration: float) -> StoreFence | None:
        """Claim only absent or expired ownership, increasing the epoch."""
        timing = _Time(now=now, duration=duration)
        candidate = StoreFence(host_id=host_id, epoch=1, expires_at=timing.now + timing.duration)
        with self._transaction():
            document = self._read()
            current = document.fence
            if timing.now < document.observed_at or (
                current is not None and timing.now < current.expires_at
            ):
                return None
            fence = candidate.model_copy(
                update={"epoch": 1 if current is None else current.epoch + 1}
            )
            self._write_lease(
                document.model_copy(update={"fence": fence, "observed_at": timing.now})
            )
            return fence

    def renew(self, fence: StoreFence, now: float, duration: float) -> StoreFence | None:
        """Extend matching ownership, preserving the authoritative lease."""
        timing = _Time(now=now, duration=duration)
        with self._transaction():
            document = self._read()
            current = document.fence
            if current is None or not _valid_fence(document, fence, timing.now):
                return None
            renewed = StoreFence(
                host_id=current.host_id,
                epoch=current.epoch,
                expires_at=max(current.expires_at, timing.now + timing.duration),
            )
            self._write_lease(
                document.model_copy(update={"fence": renewed, "observed_at": timing.now})
            )
            return renewed

    def verify(self, fence: StoreFence, now: float) -> bool:
        """Check persisted owner, epoch, expiry and time watermark."""
        timing = _Time(now=now)
        with self._transaction():
            return _valid_fence(self._read(), fence, timing.now)

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
        with self._transaction():
            document = self._read()
            revision = None if document.record is None else document.record.revision
            if revision != expected:
                return Conflict(reason=ConflictReason.REVISION, revision=revision)
            if not _valid_fence(document, fence, timing.now):
                return Conflict(reason=ConflictReason.FENCE, revision=revision)
            return self._publish(document, envelope, timing.now)

    def _publish(self, document: StoreDocument, envelope: StoreRecord, now: float) -> CommitOutcome:
        fault = next(self._faults, None)
        if fault == CommitFault.FAILED:
            message = "state-store fault plan rejected publication before writing"
            raise StateStoreWriteError(message)
        try:
            self._write_record(
                document.model_copy(update={"record": envelope, "observed_at": now}), fault
            )
        except OSError:
            return Unknown(revision=envelope.revision)
        return Committed(record=envelope)

    def _write_lease(self, document: StoreDocument) -> None:
        fault = next(self._lease_faults, None)
        self._write_record(document, fault)

    def _write_record(self, document: StoreDocument, fault: CommitFault | None) -> None:
        if fault == CommitFault.FAILED:
            message = "state-store lease fault rejected publication before writing"
            raise StateStoreWriteError(message)
        if fault == CommitFault.UNKNOWN_BEFORE:
            message = "state-store acknowledgement lost before publication"
            raise OSError(message)
        self._write(document)
        if fault in (CommitFault.UNKNOWN_AFTER, CommitFault.UNKNOWN_SYNC):
            message = "state-store acknowledgement lost after publication"
            raise OSError(message)
