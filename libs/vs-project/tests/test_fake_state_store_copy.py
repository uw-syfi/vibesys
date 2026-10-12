"""A deep copy of a fake store is a store of its own that starts where the original stood."""

from __future__ import annotations

import copy

import pytest

from vs_project.api import (
    CommitFault,
    Committed,
    FakeStateStore,
    StateStoreWriteError,
    StoredEnvelope,
)


def _envelope(revision: int) -> StoredEnvelope:
    return StoredEnvelope(revision=revision, schema_version=1, payload=b"{}")


def test_a_copy_holds_the_same_record_and_lease_and_then_diverges_from_the_original() -> None:
    store = FakeStateStore()
    fence = store.acquire("host", 0.0, 10.0)
    assert fence is not None
    assert isinstance(store.commit(None, _envelope(0), fence, 1.0), Committed)

    clone = copy.deepcopy(store)
    assert isinstance(store.commit(0, _envelope(1), fence, 2.0), Committed)

    record = clone.load()
    assert record is not None
    assert record.revision == 0
    assert isinstance(clone.commit(0, _envelope(1), fence, 2.0), Committed)
    original = store.load()
    assert original is not None
    assert original.revision == 1


def test_a_copy_has_its_own_place_in_the_fault_plan() -> None:
    store = FakeStateStore(fault_plan=[CommitFault.FAILED])
    fence = store.acquire("host", 0.0, 10.0)
    assert fence is not None
    clone = copy.deepcopy(store)

    with pytest.raises(StateStoreWriteError):
        store.commit(None, _envelope(0), fence, 1.0)

    with pytest.raises(StateStoreWriteError):
        clone.commit(None, _envelope(0), fence, 1.0)
