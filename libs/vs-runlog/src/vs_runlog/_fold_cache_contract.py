"""The cases every ``FoldCache`` passes, the file and the Fake alike.

A test module subclasses ``FoldCacheContract``, names the subclass ``Test<Variant>`` and implements
``cache_under_test``. The cases hold a cache to what a start relies on: what was stored is what is
read, a store replaces the one before whole, a value no reader would accept is not stored, and
anything the disk or a crash leaves in its place reads as a cache to ignore, never as a state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_proto.api import StateCache
from vs_runlog._fold_cache import SCHEMA_VERSION, CachedFold, CacheMiss

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_runlog._fold_cache import FoldCache

DIGEST = bytes(range(32))


@dataclass(frozen=True)
class CacheUnderTest:
    """A fresh cache, and the means to leave in its place what a crash or a bad disk would."""

    cache: FoldCache
    leave: Callable[[bytes | None], None]
    """Replace what the cache holds with these bytes, or with nothing."""


def cached(through_seq: int = 3, state: bytes = b"state") -> CachedFold:
    return CachedFold(through_seq=through_seq, digest=DIGEST, state=state)


class FoldCacheContract:
    """Cases for ``FoldCache``. Implement :meth:`cache_under_test`."""

    def cache_under_test(self) -> CacheUnderTest:
        """A fresh, empty cache with its harness."""
        raise NotImplementedError

    def test_a_cache_nobody_stored_is_absent(self) -> None:
        assert self.cache_under_test().cache.load() is CacheMiss.ABSENT

    def test_what_was_stored_is_read_back(self) -> None:
        subject = self.cache_under_test()
        subject.cache.store(cached())
        assert subject.cache.load() == cached()

    def test_a_store_replaces_the_one_before_whole(self) -> None:
        subject = self.cache_under_test()
        subject.cache.store(cached(2, b"first state, which is the longer of the two"))
        subject.cache.store(cached(5, b"second"))
        assert subject.cache.load() == cached(5, b"second")

    def test_nothing_left_in_its_place_reads_as_a_state(self) -> None:
        wire = StateCache(
            schema_version=SCHEMA_VERSION, through_seq=3, digest=DIGEST, state=b"state"
        ).to_binary()
        left = [
            b"",
            b"\x00",
            b"not a cache",
            wire[: len(wire) // 2],
            wire + b"\xff",
            StateCache(
                schema_version=SCHEMA_VERSION + 1, through_seq=3, digest=DIGEST, state=b"s"
            ).to_binary(),
            StateCache(
                schema_version=SCHEMA_VERSION, through_seq=0, digest=DIGEST, state=b"s"
            ).to_binary(),
            StateCache(
                schema_version=SCHEMA_VERSION, through_seq=3, digest=b"short", state=b"s"
            ).to_binary(),
        ]
        for data in left:
            subject = self.cache_under_test()
            subject.cache.store(cached())
            subject.leave(data)
            assert subject.cache.load() is CacheMiss.UNREADABLE, data

    def test_a_deleted_cache_is_absent(self) -> None:
        subject = self.cache_under_test()
        subject.cache.store(cached())
        subject.leave(None)
        assert subject.cache.load() is CacheMiss.ABSENT

    def test_a_value_no_reader_would_accept_is_refused_and_leaves_the_cache_as_it_was(self) -> None:
        subject = self.cache_under_test()
        subject.cache.store(cached())
        for invalid in (
            CachedFold(through_seq=0, digest=DIGEST, state=b"state"),
            CachedFold(through_seq=3, digest=b"short", state=b"state"),
            CachedFold(through_seq=3, digest=DIGEST, state=b""),
        ):
            assert _refused(subject.cache, invalid), invalid
            assert subject.cache.load() == cached()


def _refused(cache: FoldCache, value: CachedFold) -> bool:
    try:
        cache.store(value)
    except ValueError:
        return True
    return False
