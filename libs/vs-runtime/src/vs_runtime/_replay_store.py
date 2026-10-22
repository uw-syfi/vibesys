"""Replay persistence: native run.log records, with the existing project lease."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vs_core.api.replay import ReplayArchive
from vs_project.api import Committed, Conflict, ConflictReason, StoredEnvelope, Unknown
from vs_proto.api import ReplayStore as ReplayRecords

if TYPE_CHECKING:
    from vs_project.api import CommitOutcome, StateStore, StoreFence, StoreRecord
    from vs_runlog.api import FileRunLogWriter


REPLAY_STORE_SCHEMA_VERSION = 2


class ReplayStore(Protocol):
    """The recorder's append and writer-lease requirements.

    Callers submit one-record extensions of the immutable prefix. A confirmed
    commit installs the entire submitted prefix; an uncertain commit authorizes
    no dispatch. Loading recovers the complete prefix. Existing StateStores also
    satisfy this surface during migration.
    """

    def load(self) -> StoreRecord | None: ...

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome: ...

    def acquire(self, host_id: str, now: float, duration: float) -> StoreFence | None: ...

    def renew(self, fence: StoreFence, now: float, duration: float) -> StoreFence | None: ...

    def release(self, fence: StoreFence, now: float) -> bool: ...

    def verify(self, fence: StoreFence, now: float) -> bool: ...


class RunLogReplayStore:
    """Use run.log as authority; the project store supplies only the writer lease.

    The caller owns the writer's lifetime and opens its path through Project.
    No replay envelope is written to the lease store. The returned envelope is
    a disposable view, allowing the recorder's migration-era codec to remain
    private. A writer error is ambiguous even when bytes are already readable.
    """

    def __init__(self, writer: FileRunLogWriter, leases: StateStore) -> None:
        if leases.load() is not None:
            problem = "native replay requires facts-only import of the existing state store"
            raise ValueError(problem)
        self._writer, self._leases = writer, leases

    def load(self) -> StoredEnvelope | None:
        records = self._writer.read()
        if not records:
            return None
        payload = ReplayRecords(records=list(records)).to_binary()
        ReplayArchive.decode(payload)
        return StoredEnvelope(
            revision=len(records) - 1, schema_version=REPLAY_STORE_SCHEMA_VERSION, payload=payload
        )

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome:
        """Validate the immutable prefix, then fence append and fsync together."""
        current = self.load()
        revision = None if current is None else current.revision
        if revision != expected_revision:
            return Conflict(reason=ConflictReason.REVISION, revision=revision)
        expected = 0 if revision is None else revision + 1
        if envelope.revision != expected or envelope.schema_version != REPLAY_STORE_SCHEMA_VERSION:
            problem = "replay append requires the next revision and current schema"
            raise ValueError(problem)
        ReplayArchive.decode(envelope.payload)
        proposed = ReplayRecords.from_binary(envelope.payload).records
        retained = () if current is None else ReplayRecords.from_binary(current.payload).records
        if len(proposed) != len(retained) + 1 or any(
            old.to_binary() != new.to_binary() for old, new in zip(retained, proposed, strict=False)
        ):
            problem = "replay commit must append one record without rewriting history"
            raise ValueError(problem)

        def append() -> None:
            self._writer.append(proposed[-1])
            self._writer.commit()

        try:
            if not self._leases.mutate_if_fenced(fence, now, append):
                return Conflict(reason=ConflictReason.FENCE, revision=revision)
        except OSError:
            return Unknown(revision=envelope.revision)
        return Committed(record=envelope)

    def acquire(self, host_id: str, now: float, duration: float) -> StoreFence | None:
        return self._leases.acquire(host_id, now, duration)

    def renew(self, fence: StoreFence, now: float, duration: float) -> StoreFence | None:
        return self._leases.renew(fence, now, duration)

    def release(self, fence: StoreFence, now: float) -> bool:
        return self._leases.release(fence, now)

    def verify(self, fence: StoreFence, now: float) -> bool:
        return self._leases.verify(fence, now)
