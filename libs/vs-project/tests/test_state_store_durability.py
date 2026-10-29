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
    LocalStateStore,
    ObservationFault,
    Project,
    StateDocumentDamagedError,
    StoredEnvelope,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vs_project.api import AtomicWriteStream


class _CountingEffects(LocalAtomicWriteEffects):
    """Local effects that count every durability operation."""

    def __init__(self) -> None:
        self.syncs = 0
        self.reloaded: list[str] = []

    def sync_file(self, stream: AtomicWriteStream) -> None:
        self.syncs += 1
        super().sync_file(stream)

    def sync_existing_file(self, path: Path) -> None:
        self.syncs += 1
        self.reloaded.append(path.name)
        super().sync_existing_file(path)

    def sync_directory(self, directory: Path) -> None:
        self.syncs += 1
        super().sync_directory(directory)


def _envelope(revision: int, payload: bytes = b"p") -> StoredEnvelope:
    return StoredEnvelope(revision=revision, schema_version=1, payload=payload)


@given(reads=st.integers(min_value=1, max_value=6))
@settings(max_examples=examples(pr=5, nightly=10), deadline=None)
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


def test_an_unfinished_publication_is_synchronized_before_it_is_read(tmp_path: Path) -> None:
    effects = _CountingEffects()
    store = Project.open(tmp_path).state_store(
        "run-1", fault_plan=(CommitFault.UNKNOWN_SYNC,), effects=effects
    )
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    store.commit(None, _envelope(0), fence, now=1)

    assert store.load() == _envelope(0)

    # The commit that lost its acknowledgement reloads the record to learn whether it was
    # published, and that reload synchronizes it; a clean publication never reloads.
    assert "store.json" in effects.reloaded


def test_an_unfinished_lease_publication_is_synchronized_before_it_is_trusted(
    tmp_path: Path,
) -> None:
    effects = _CountingEffects()
    store = Project.open(tmp_path).state_store(
        "run-1", lease_fault_plan=(None, CommitFault.UNKNOWN_SYNC), effects=effects
    )
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    # The renewal is published, but syncing its directory fails, so the host sees an error.
    with pytest.raises(OSError, match="synchronization failed"):
        store.renew(fence, now=1, duration=10)

    assert store.verify(fence, now=2)

    # The renewal's bytes may not be durable, so the next read of the lease synchronizes
    # them before the host acts under the lease they name.
    assert "lease.json" in effects.reloaded


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


class _CountingDecoder:
    """Counts how many documents a store was asked to decode."""

    def __init__(self) -> None:
        self.decodes = 0

    def __call__(self) -> None:
        self.decodes += 1


@given(commits=st.integers(min_value=1, max_value=12), payload_size=st.integers(0, 2000))
@settings(max_examples=examples(pr=5, nightly=10), deadline=None)
def test_a_commit_decodes_no_document_whatever_its_size_or_the_commits_before_it(
    commits: int, payload_size: int
) -> None:
    with TemporaryDirectory() as directory:
        decoder = _CountingDecoder()
        store = LocalStateStore(Project.open(Path(directory)), "run-1", on_decode=decoder)
        fence = store.acquire("host-a", now=0, duration=10)
        assert fence is not None
        previous: int | None = None
        for revision in range(commits):
            envelope = _envelope(revision, b"p" * payload_size)
            assert store.commit(previous, envelope, fence, now=1) == Committed(record=envelope)
            previous = revision
        assert store.load() == _envelope(commits - 1, b"p" * payload_size)
        assert decoder.decodes == 0


def test_a_document_another_store_replaced_is_decoded_not_remembered(tmp_path: Path) -> None:
    decoder = _CountingDecoder()
    reader = LocalStateStore(Project.open(tmp_path), "run-1", on_decode=decoder)
    writer = Project.open(tmp_path).state_store("run-1")
    fence = writer.acquire("host-a", now=0, duration=10)
    assert fence is not None
    writer.commit(None, _envelope(0), fence, now=1)
    assert reader.load() == _envelope(0)
    assert reader.load() == _envelope(0)
    assert decoder.decodes == 1

    writer.commit(0, _envelope(1), fence, now=2)

    assert reader.load() == _envelope(1)
    assert decoder.decodes == 2


def test_damaged_bytes_are_refused_after_a_good_document_was_remembered(tmp_path: Path) -> None:
    store = Project.open(tmp_path).state_store("run-1")
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    store.commit(None, _envelope(0), fence, now=1)
    assert store.load() == _envelope(0)
    namespace = Project.open(tmp_path).state.state_store_namespace("run-1")
    namespace.write_bytes("store.json", b'{"version":')

    with pytest.raises(StateDocumentDamagedError):
        store.load()
