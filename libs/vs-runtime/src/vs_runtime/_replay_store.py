"""Keyed replay persistence with run.log as the only record authority."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vs_project.api import Committed, Conflict, ConflictReason, StoredEnvelope, Unknown
from vs_proto.api import ReplayStore as ReplayRecords

if TYPE_CHECKING:
    from vs_project.api import CommitOutcome, StateStore, StoreFence, StoreRecord
    from vs_proto.api import Record
    from vs_runlog.api import FileRunLogWriter


REPLAY_STORE_SCHEMA_VERSION = 2

_FORMAT_FENCE = StoredEnvelope(revision=0, schema_version=REPLAY_STORE_SCHEMA_VERSION, payload=b"")
"""The atomic store's only keyed record: no scheduling state, a version old launchers reject."""


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
    """Commit keyed replay records to run.log, the only authority for them.

    The caller owns the writer's lifetime and opens its path through Project.
    The atomic store holds two durable facts: the writer lease, and a format
    fence installed before the first record so every positional launcher fails
    closed. It never holds scheduling state and never repairs the log. Load
    returns the committed log prefix; deleted caches (head, keyframes) change
    nothing. The writer drops only a torn final frame; complete corruption fails.
    Committed follows log durability; a write or sync error is Unknown, which
    authorizes no dispatch. Under one fence only this store appends, so the
    prefix it confirmed stays current and a commit costs its own record; a new
    fence or an uncertain append reads the log again.
    """

    def __init__(self, writer: FileRunLogWriter, store: StateStore) -> None:
        self._writer, self._store = writer, store
        self._confirmed: tuple[StoreFence, StoredEnvelope | None] | None = None

    def load(self) -> StoreRecord | None:
        stored = self._store.load()
        if (
            isinstance(stored, StoredEnvelope)
            and stored.schema_version == REPLAY_STORE_SCHEMA_VERSION
            and stored != _FORMAT_FENCE
        ):
            problem = "the atomic store holds keyed replay records; only run.log may hold them"
            raise ValueError(problem)
        if stored is not None and stored != _FORMAT_FENCE:
            # A retained positional run, whose run.log is not a keyed prefix either.
            return stored
        records = self._writer.read()
        if not records:
            return None
        if stored is None:
            problem = "run.log has keyed replay records but no legacy-dispatch format fence"
            raise ValueError(problem)
        return _envelope(records)

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome:
        """Append exactly the one record that extends the confirmed log prefix."""
        if envelope.schema_version != REPLAY_STORE_SCHEMA_VERSION:
            problem = "keyed replay requires facts-only import of the retained store format"
            raise ValueError(problem)
        if not self._store.verify(fence, now):
            self._confirmed = None
            return Conflict(reason=ConflictReason.FENCE, revision=None)
        confirmed = self._prefix(fence)
        revision = None if confirmed is None else confirmed.revision
        if expected_revision != revision or envelope.revision != (
            0 if revision is None else revision + 1
        ):
            return Conflict(reason=ConflictReason.REVISION, revision=revision)
        record = _extension(confirmed, envelope)
        if confirmed is None and (outcome := self._install_fence(fence, now)) is not None:
            return outcome
        self._confirmed = None
        try:
            self._writer.append(record)
            self._writer.commit()
        except OSError:
            return Unknown(revision=envelope.revision)
        prefix = b"" if confirmed is None else confirmed.payload
        extended = StoredEnvelope(
            revision=envelope.revision,
            schema_version=REPLAY_STORE_SCHEMA_VERSION,
            payload=prefix + ReplayRecords(records=[record]).to_binary(),
        )
        self._confirmed = fence, extended
        return Committed(record=envelope)

    def _prefix(self, fence: StoreFence) -> StoredEnvelope | None:
        """The confirmed log prefix, read again unless this fence's own appends confirmed it."""
        if self._confirmed is None or self._confirmed[0] != fence:
            records = self._writer.read()
            self._confirmed = fence, _envelope(records) if records else None
        return self._confirmed[1]

    def _install_fence(self, fence: StoreFence, now: float) -> CommitOutcome | None:
        stored = self._store.load()
        if stored == _FORMAT_FENCE:
            return None
        if stored is not None:
            problem = "keyed replay requires facts-only import of the retained store format"
            raise ValueError(problem)
        outcome = self._store.commit(None, _FORMAT_FENCE, fence, now)
        return None if isinstance(outcome, Committed) else outcome

    def acquire(self, host_id: str, now: float, duration: float) -> StoreFence | None:
        return self._store.acquire(host_id, now, duration)

    def renew(self, fence: StoreFence, now: float, duration: float) -> StoreFence | None:
        return self._store.renew(fence, now, duration)

    def release(self, fence: StoreFence, now: float) -> bool:
        return self._store.release(fence, now)

    def verify(self, fence: StoreFence, now: float) -> bool:
        return self._store.verify(fence, now)


def _envelope(records: tuple[Record, ...]) -> StoredEnvelope:
    return StoredEnvelope(
        revision=len(records) - 1,
        schema_version=REPLAY_STORE_SCHEMA_VERSION,
        payload=ReplayRecords(records=list(records)).to_binary(),
    )


def _extension(confirmed: StoredEnvelope | None, envelope: StoredEnvelope) -> Record:
    prefix = b"" if confirmed is None else confirmed.payload
    added = (
        ReplayRecords.from_binary(envelope.payload[len(prefix) :]).records
        if envelope.payload.startswith(prefix)
        else []
    )
    if len(added) != 1:
        problem = "a replay commit must extend the confirmed run.log prefix by one record"
        raise ValueError(problem)
    return added[0]
