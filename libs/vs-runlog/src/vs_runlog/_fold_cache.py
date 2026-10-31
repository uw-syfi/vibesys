"""state.pb: a cache of the fold of a run.log, and the rule for when a start may use it.

The cache holds the bytes of a fold (the writer's own encoding, opaque here), the number of records
it covers and the digest of those records. It is never authoritative: a start that finds it absent,
unreadable, ahead of the log or not the fold of the log's own first records folds the whole log, and
the result is the same. Deleting it, corrupting it or truncating the log under it changes no result.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from vs_proto.api import StateCache, validate
from vs_runlog._atomic import FileSync, OsFileSync, publish

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from vs_proto.api import Record

SCHEMA_VERSION = 1
"""The version of the meaning of ``state.pb``; a file of another version is ignored."""


class CacheMiss(StrEnum):
    """Why a start folds the whole log instead of the records after a cache. Closed."""

    ABSENT = "absent"
    """There is no cache."""
    UNREADABLE = "unreadable"
    """The file does not parse, does not validate or is of another version."""
    AHEAD_OF_LOG = "ahead_of_log"
    """The cache covers more records than the log holds."""
    DIGEST_MISMATCH = "digest_mismatch"
    """The cache is not the fold of the log's own first records."""
    STATE_REFUSED = "state_refused"
    """The fold in the cache could not be read by the code that wrote it."""


@dataclass(frozen=True)
class CachedFold:
    """A fold, the first ``through_seq`` records it covers, and the digest of those records."""

    through_seq: int
    digest: bytes
    state: bytes


class FoldCache(Protocol):
    """Where the fold of a run is cached. A read never fails the run, and a write is atomic."""

    def load(self) -> CachedFold | CacheMiss:
        """The cache as stored, or why there is none to use. It is not yet judged against a log."""
        ...

    def store(self, cached: CachedFold) -> None:
        """Replace the cache with ``cached``: a reader sees the whole of it or the one before.

        Raises ``OSError`` when the write fails; the cache before it stays.
        """
        ...


class PrefixDigest:
    """The digest of the first records of a log, extended one record at a time."""

    def __init__(self) -> None:
        """Start with no records."""
        self._hash = hashlib.sha256()
        self._count = 0

    @property
    def count(self) -> int:
        """How many records the digest covers."""
        return self._count

    def extend(self, records: Iterable[Record]) -> None:
        """Cover ``records``, which follow those already covered."""
        for record in records:
            self._hash.update(record.to_binary())
            self._count += 1

    def value(self) -> bytes:
        """The digest of the records covered so far."""
        return self._hash.copy().digest()


def judge(
    loaded: CachedFold | CacheMiss, records: Sequence[Record], digest: PrefixDigest
) -> CachedFold | CacheMiss:
    """``loaded`` if it is the cache of ``records``' own first records, else why not.

    ``digest`` must be empty; it covers every record of ``records`` afterwards, whatever the
    verdict, so a writer can go on from the end of the log.
    """
    if not isinstance(loaded, CachedFold):
        digest.extend(records)
        return loaded
    if loaded.through_seq > len(records):
        digest.extend(records)
        return CacheMiss.AHEAD_OF_LOG
    digest.extend(records[: loaded.through_seq])
    verdict = loaded if digest.value() == loaded.digest else CacheMiss.DIGEST_MISMATCH
    digest.extend(records[loaded.through_seq :])
    return verdict


def _decoded(data: bytes) -> CachedFold | CacheMiss:
    """The cache a ``state.pb`` holds, or ``UNREADABLE`` when it is not one of this version."""
    try:
        message = StateCache.from_binary(data)
        validate(message)
    except ValueError:
        return CacheMiss.UNREADABLE
    if message.schema_version != SCHEMA_VERSION:
        return CacheMiss.UNREADABLE
    return CachedFold(message.through_seq, message.digest, message.state)


def _encoded(cached: CachedFold) -> bytes:
    """The ``state.pb`` bytes of ``cached``, validated as a reader will."""
    message = StateCache(
        schema_version=SCHEMA_VERSION,
        through_seq=cached.through_seq,
        digest=cached.digest,
        state=cached.state,
    )
    validate(message)
    return message.to_binary()


class FileFoldCache:
    """``state.pb`` on disk, replaced with a temporary file, an fsync and a rename."""

    def __init__(self, path: Path, *, sync: FileSync | None = None) -> None:
        """Cache in ``path``; ``sync`` is the durability operation, the operating system's by default."""
        self._path = path
        self._sync = OsFileSync() if sync is None else sync

    def load(self) -> CachedFold | CacheMiss:
        """The file as stored; a missing file is ``ABSENT``, anything else unusable ``UNREADABLE``."""
        try:
            return _decoded(self._path.read_bytes())
        except FileNotFoundError:
            return CacheMiss.ABSENT
        except OSError:
            return CacheMiss.UNREADABLE

    def store(self, cached: CachedFold) -> None:
        """Replace the file with ``cached``."""
        publish(self._path, _encoded(cached), sync=self._sync)


class MemoryFoldCache:
    """A cache kept in memory, as strict as the file: what it stores it validates and re-reads."""

    def __init__(self) -> None:
        """Start with no cache."""
        self._data: bytes | None = None

    def load(self) -> CachedFold | CacheMiss:
        """The cache as stored."""
        return CacheMiss.ABSENT if self._data is None else _decoded(self._data)

    def store(self, cached: CachedFold) -> None:
        """Replace the cache with ``cached``."""
        self._data = _encoded(cached)

    def damage(self, data: bytes | None) -> None:
        """Replace the stored bytes with anything, or delete them (``None``): a crash, a bad disk."""
        self._data = data
