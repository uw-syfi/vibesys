"""Replay persistence on the existing atomic store, with native log publication."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vs_core.api.replay import ReplayArchive
from vs_project.api import Committed, StoredEnvelope, Unknown
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
    """Publish confirmed store records to run.log under the caller's writer ownership.

    The caller owns the writer's lifetime and opens its path through Project.
    The atomic store remains authoritative during P2. Publication failure stops
    dispatch; load repairs a missing projection from the confirmed prefix. The
    envelope's unsupported version fences old scheduling readers. P3 can replace
    this implementation when native-log authority and writer exclusion land.
    """

    def __init__(self, writer: FileRunLogWriter, store: StateStore) -> None:
        self._writer, self._store = writer, store

    def load(self) -> StoreRecord | None:
        stored = self._store.load()
        if (
            isinstance(stored, StoredEnvelope)
            and stored.schema_version == REPLAY_STORE_SCHEMA_VERSION
        ):
            self._publish(stored)
        elif stored is None and self._writer.read():
            problem = "run.log has records but its authoritative replay store is missing"
            raise ValueError(problem)
        return stored

    def _publish(self, envelope: StoredEnvelope) -> None:
        if envelope.schema_version != REPLAY_STORE_SCHEMA_VERSION:
            problem = "keyed replay requires facts-only import of the retained store format"
            raise ValueError(problem)
        ReplayArchive.decode(envelope.payload)
        proposed = ReplayRecords.from_binary(envelope.payload).records
        retained = self._writer.read()
        if len(retained) > len(proposed) or any(
            old.to_binary() != new.to_binary() for old, new in zip(retained, proposed, strict=False)
        ):
            problem = "run.log contradicts the authoritative replay store prefix"
            raise ValueError(problem)
        for record in proposed[len(retained) :]:
            self._writer.append(record)
        self._writer.commit()

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome:
        """Commit authority first; only confirmed native publication permits dispatch."""
        outcome = self._store.commit(expected_revision, envelope, fence, now)
        if not isinstance(outcome, Committed):
            return outcome
        try:
            self._publish(envelope)
        except OSError:
            return Unknown(revision=envelope.revision)
        return outcome

    def acquire(self, host_id: str, now: float, duration: float) -> StoreFence | None:
        return self._store.acquire(host_id, now, duration)

    def renew(self, fence: StoreFence, now: float, duration: float) -> StoreFence | None:
        return self._store.renew(fence, now, duration)

    def release(self, fence: StoreFence, now: float) -> bool:
        return self._store.release(fence, now)

    def verify(self, fence: StoreFence, now: float) -> bool:
        return self._store.verify(fence, now)
