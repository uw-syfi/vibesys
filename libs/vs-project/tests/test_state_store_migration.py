"""A store written by an older release, with the lease inside ``store.json``, keeps working.

Older releases kept the record and the lease in one ``store.json`` (version 1). This release
keeps the lease in ``lease.json`` so a renewal does not rewrite the record (FINDINGS S171). A
run started by an older release and resumed by this one must keep its record, its fence, its
epoch and its time watermark, and the first lease write moves the lease out.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vs_project.api import (
    FakeStateStore,
    Project,
    StateDocumentDamagedError,
    StoredEnvelope,
    StoreFence,
    StoreFile,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_project.api import StateStore

# The exact bytes origin/main (4512fef68) LocalStateStore wrote after acquire at 100 for 30 s,
# commit at 101, renew at 110 for 30 s and a second commit at 112.5.
_V1_STORE = (
    b'{"version":1,"record":{"kind":"envelope","revision":1,"schema_version":3,"payload":'
    b'"eyJlbnZlbG9wZSI6eyJjb3JlIjp7InJ1biI6eyJzdGF0dXMiOiJhY3RpdmUiLCJyZXZpc2lvbiI6MX19fX0="},'
    b'"fence":{"host_id":"host-a","epoch":1,"expires_at":140.0},"observed_at":112.5}'
)
_V1_RECORD = StoredEnvelope(
    revision=1,
    schema_version=3,
    payload=b'{"envelope":{"core":{"run":{"status":"active","revision":1}}}}',
)
_V1_FENCE = StoreFence(host_id="host-a", epoch=1, expires_at=140.0)


class _Files:
    """One store over persisted documents that a test writes and reads by hand."""

    def __init__(self, *, local: bool, root: Path) -> None:
        self._fake = None if local else FakeStateStore()
        self._root = root

    def store(self) -> StateStore:
        """The store, reopened from the persisted documents for a local store."""
        return self._fake or Project.open(self._root).state_store("run-1")

    def write(self, file: StoreFile, contents: bytes) -> None:
        if self._fake is not None:
            self._fake.replace_document(contents, file=file)
            return
        self._directory().mkdir(parents=True, exist_ok=True)
        (self._directory() / file).write_bytes(contents)

    def read(self, file: StoreFile) -> dict[str, object] | None:
        """A local store's document as JSON, or None when it does not exist."""
        assert self._fake is None, "only a local store's documents are files to read"
        path = self._directory() / file
        return json.loads(path.read_bytes()) if path.exists() else None

    def _directory(self) -> Path:
        return Project.open(self._root).state.state_store_namespace("run-1").external_directory()


@pytest.fixture(params=[False, True], ids=["fake", "local"])
def files(request: pytest.FixtureRequest, tmp_path: Path) -> _Files:
    stored = _Files(local=request.param, root=tmp_path)
    stored.write(StoreFile.RECORD, _V1_STORE)
    return stored


def test_an_older_store_keeps_its_record_fence_epoch_and_watermark(files: _Files) -> None:
    store = files.store()
    assert store.load() == _V1_RECORD
    assert store.verify(_V1_FENCE, now=120)
    # The watermark of the last published write still refuses an earlier clock.
    assert not store.verify(_V1_FENCE, now=112)
    assert store.acquire("host-b", now=139, duration=10) is None
    taken = store.acquire("host-b", now=140, duration=10)
    assert taken == StoreFence(host_id="host-b", epoch=2, expires_at=150)
    assert not store.verify(_V1_FENCE, now=141)


_LEASE_WRITES: dict[str, Callable[[StateStore], object]] = {
    "renew": lambda store: store.renew(_V1_FENCE, now=120, duration=30),
    "release": lambda store: store.release(_V1_FENCE, now=120),
    "commit": lambda store: store.commit(
        1, StoredEnvelope(revision=2, schema_version=3, payload=b"next"), _V1_FENCE, now=120
    ),
}


@pytest.mark.parametrize("write", sorted(_LEASE_WRITES))
def test_the_first_write_moves_the_lease_out_of_the_record(tmp_path: Path, write: str) -> None:
    files = _Files(local=True, root=tmp_path)
    files.write(StoreFile.RECORD, _V1_STORE)
    _LEASE_WRITES[write](files.store())
    lease = files.read(StoreFile.LEASE)
    record = files.read(StoreFile.RECORD)
    assert lease is not None
    assert record is not None
    assert lease["observed_at"] == 120
    assert record["version"] == 2
    assert set(record) == {"version", "record"}
    reopened = files.store()
    assert (reopened.load() or _V1_RECORD).revision == (2 if write == "commit" else 1)
    assert reopened.verify(_V1_FENCE, now=121) == (write != "release")


def test_a_lease_file_beside_an_older_record_is_the_authority(files: _Files) -> None:
    """A crash between moving the lease out and rewriting the record leaves both copies."""
    newer = StoreFence(host_id="host-b", epoch=2, expires_at=200)
    files.write(
        StoreFile.LEASE,
        b'{"version":1,"fence":' + newer.model_dump_json().encode() + b',"observed_at":150}',
    )
    store = files.store()
    assert not store.verify(_V1_FENCE, now=120)
    assert store.verify(newer, now=160)
    assert store.load() == _V1_RECORD


def test_a_current_record_without_its_lease_is_damage(files: _Files) -> None:
    """Inventing an empty lease would let the next host start again at epoch 1."""
    files.write(StoreFile.RECORD, b'{"version":2,"record":null}')
    store = files.store()
    with pytest.raises(StateDocumentDamagedError, match=r"lease\.json"):
        store.acquire("host-b", now=500, duration=10)


def test_a_current_record_cannot_carry_a_lease(files: _Files) -> None:
    files.write(StoreFile.RECORD, _V1_STORE.replace(b'"version":1', b'"version":2', 1))
    with pytest.raises(StateDocumentDamagedError, match=r"store\.json"):
        files.store().load()
