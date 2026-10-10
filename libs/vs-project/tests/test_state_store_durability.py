"""A reload synchronizes only what this store has not already made durable."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_project.api import (
    CommitFault,
    Committed,
    LocalAtomicWriteEffects,
    ObservationFault,
    Project,
    StoredEnvelope,
)

if TYPE_CHECKING:
    from vs_project.api import AtomicWriteStream


class _CountingEffects(LocalAtomicWriteEffects):
    """Local effects that count every durability operation."""

    def __init__(self) -> None:
        self.syncs = 0

    def sync_file(self, stream: AtomicWriteStream) -> None:
        self.syncs += 1
        super().sync_file(stream)

    def sync_existing_file(self, path: Path) -> None:
        self.syncs += 1
        super().sync_existing_file(path)

    def sync_directory(self, directory: Path) -> None:
        self.syncs += 1
        super().sync_directory(directory)


def _envelope(revision: int, payload: bytes = b"p") -> StoredEnvelope:
    return StoredEnvelope(revision=revision, schema_version=1, payload=payload)


@given(reads=st.integers(min_value=1, max_value=6))
@settings(max_examples=10, deadline=None)
def test_reloading_bytes_this_store_published_synchronizes_nothing(reads: int) -> None:
    with TemporaryDirectory() as directory:
        effects = _CountingEffects()
        store = Project.open(Path(directory)).state_store("run-1", effects=effects)
        fence = store.acquire("host-a", now=0, duration=10)
        assert fence is not None
        assert store.commit(None, _envelope(0), fence, now=1) == Committed(record=_envelope(0))
        after_commit = effects.syncs
        for _ in range(reads):
            assert store.load() == _envelope(0)
            assert store.verify(fence, now=2)
        assert effects.syncs == after_commit


def test_reloading_bytes_another_store_published_synchronizes_them_once(tmp_path: Path) -> None:
    effects = _CountingEffects()
    reader = Project.open(tmp_path).state_store("run-1", effects=effects)
    writer = Project.open(tmp_path).state_store("run-1")
    fence = writer.acquire("host-a", now=0, duration=10)
    assert fence is not None
    assert writer.commit(None, _envelope(0), fence, now=1) == Committed(record=_envelope(0))

    assert reader.load() == _envelope(0)
    first = effects.syncs
    assert first > 0
    assert reader.load() == _envelope(0)
    assert effects.syncs == first

    assert writer.commit(0, _envelope(1), fence, now=2) == Committed(record=_envelope(1))
    assert reader.load() == _envelope(1)
    assert effects.syncs > first


def test_an_unfinished_publication_is_synchronized_by_the_next_reload(tmp_path: Path) -> None:
    effects = _CountingEffects()
    store = Project.open(tmp_path).state_store(
        "run-1", fault_plan=(CommitFault.UNKNOWN_SYNC,), effects=effects
    )
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    store.commit(None, _envelope(0), fence, now=1)
    before = effects.syncs

    assert store.load() == _envelope(0)

    assert effects.syncs > before


def test_an_observation_sync_fault_still_fires_on_already_durable_bytes(tmp_path: Path) -> None:
    store = Project.open(tmp_path).state_store(
        "run-1", observation_fault_plan=(None, None, None, ObservationFault.SYNC)
    )
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    store.commit(None, _envelope(0), fence, now=1)
    assert store.load() == _envelope(0)

    with pytest.raises(OSError, match="synchronization failed"):
        store.load()
    assert store.load() == _envelope(0)
