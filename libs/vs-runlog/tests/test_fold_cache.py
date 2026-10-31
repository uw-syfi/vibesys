"""state.pb is a cache: stored whole, judged against the log it claims to cover, never trusted.

The file and its Fake pass one contract. A cache is used only if it is the fold of the log's own
first records, and the digest a writer goes on from stands at the end of the log whatever the
verdict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_proto.api import CoreInput, Oneof, Record, Timestamp
from vs_runlog.api import (
    CachedFold,
    CacheMiss,
    CacheUnderTest,
    FakeFileSync,
    FileFoldCache,
    FoldCacheContract,
    MemoryFoldCache,
    PrefixDigest,
    judge,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestFileFoldCache(FoldCacheContract):
    @pytest.fixture(autouse=True)
    def _directory(self, tmp_path: Path) -> None:
        self.directory = tmp_path
        self.made = 0

    def cache_under_test(self) -> CacheUnderTest:
        self.made += 1
        home = self.directory / str(self.made)
        home.mkdir()
        path = home / "state.pb"

        def leave(data: bytes | None) -> None:
            if data is None:
                path.unlink()
            else:
                path.write_bytes(data)

        return CacheUnderTest(FileFoldCache(path, sync=FakeFileSync()), leave)


class TestMemoryFoldCache(FoldCacheContract):
    def cache_under_test(self) -> CacheUnderTest:
        cache = MemoryFoldCache()
        return CacheUnderTest(cache, cache.damage)


def entry(seq: int, salt: bytes) -> Record:
    return Record(
        seq=seq,
        epoch=1,
        at=Timestamp(seconds=seq),
        clock=float(seq),
        body=Oneof("core_input", CoreInput(schema_version=1, event=salt)),
    )


LOGS = st.lists(st.binary(min_size=1, max_size=6), min_size=1, max_size=12).map(
    lambda salts: [entry(index + 1, salt) for index, salt in enumerate(salts)]
)


def digest_of(records: list[Record]) -> bytes:
    digest = PrefixDigest()
    digest.extend(records)
    return digest.value()


@given(records=LOGS, data=st.data())
def test_a_cache_of_the_logs_own_first_records_is_used_and_the_digest_stands_at_its_end(
    records: list[Record], data: st.DataObject
) -> None:
    covered = data.draw(st.integers(1, len(records)), label="records the cache covers")
    cache = CachedFold(covered, digest_of(records[:covered]), b"state")
    digest = PrefixDigest()
    assert judge(cache, records, digest) == cache
    assert digest.count == len(records)
    assert digest.value() == digest_of(records)


@given(records=LOGS, data=st.data())
def test_a_cache_of_a_log_that_differs_in_its_first_records_is_not_used(
    records: list[Record], data: st.DataObject
) -> None:
    covered = data.draw(st.integers(1, len(records)), label="records the cache covers")
    changed = data.draw(st.integers(0, covered - 1), label="record that differs")
    other = list(records)
    other[changed] = entry(changed + 1, records[changed].body.value.event + b"!")
    cache = CachedFold(covered, digest_of(other[:covered]), b"state")
    digest = PrefixDigest()
    assert judge(cache, records, digest) is CacheMiss.DIGEST_MISMATCH
    assert digest.value() == digest_of(records)


@given(records=LOGS, extra=st.integers(1, 5))
def test_a_cache_that_covers_more_than_the_log_holds_is_ahead_of_it(
    records: list[Record], extra: int
) -> None:
    cache = CachedFold(len(records) + extra, digest_of(records), b"state")
    digest = PrefixDigest()
    assert judge(cache, records, digest) is CacheMiss.AHEAD_OF_LOG
    assert digest.value() == digest_of(records)


@given(records=LOGS, miss=st.sampled_from([CacheMiss.ABSENT, CacheMiss.UNREADABLE]))
def test_a_cache_there_is_none_to_use_is_its_own_verdict(
    records: list[Record], miss: CacheMiss
) -> None:
    digest = PrefixDigest()
    assert judge(miss, records, digest) is miss
    assert digest.value() == digest_of(records)


@given(records=LOGS, data=st.data())
def test_a_digest_extended_in_pieces_is_the_digest_of_the_whole(
    records: list[Record], data: st.DataObject
) -> None:
    cut = data.draw(st.integers(0, len(records)))
    digest = PrefixDigest()
    digest.extend(records[:cut])
    digest.extend(records[cut:])
    assert digest.value() == digest_of(records)
    assert digest.count == len(records)


def test_a_cache_in_a_file_survives_a_new_reader(tmp_path: Path) -> None:
    path = tmp_path / "state.pb"
    FileFoldCache(path, sync=FakeFileSync()).store(CachedFold(7, bytes(32), b"state"))
    assert FileFoldCache(path).load() == CachedFold(7, bytes(32), b"state")
    assert [entry.name for entry in tmp_path.iterdir()] == ["state.pb"]
