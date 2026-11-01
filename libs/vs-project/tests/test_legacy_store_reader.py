"""The record a run saved before the step loop left in ``store.json`` reads the same from every store.

Nothing writes ``store.json`` any more; the stores only read it, for the migration of such a run into
the run log and to refuse to resume one. These are the properties of that read, over the in-memory
store and the local one: what was saved reads back whole, a version 1 document (the lease inside it,
as older releases wrote it) is accepted and its lease ignored, and a document that is damaged or from
a newer release is a typed error that never echoes it.
"""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_project.api import (
    FakeStateStore,
    ObservationFault,
    Project,
    QuarantinedEnvelope,
    StateDocumentDamagedError,
    StoredEnvelope,
    StoreFence,
)
from vs_project.api.testing import seed_store_document, seed_stored_record, stored_record_bytes
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_project.api import StateStore, StoreRecord

_RUN = "run-1"

# The exact bytes a release before the lease moved to its own file wrote after acquire at 100 for
# 30 s, commit at 101, renew at 110 for 30 s and a second commit at 112.5.
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

_LEAK_MARKER = "s3cr3t-document-bytes"
_DAMAGES = {
    "truncated": b'{"version":1,"record":{"revision":0,"schema_version":1,"payload":"'
    + _LEAK_MARKER.encode(),
    "not_utf8": b"\xff\xfe\x00" + _LEAK_MARKER.encode(),
    "not_an_object": b'["' + _LEAK_MARKER.encode() + b'"]',
    "unknown_key": b'{"version":1,"written_by_newer_release":"' + _LEAK_MARKER.encode() + b'"}',
    "wrong_field_type": b'{"version":1,"observed_at":"' + _LEAK_MARKER.encode() + b'"}',
    "a_lease_in_the_current_format": b'{"version":2,"observed_at":5,"leak":"'
    + _LEAK_MARKER.encode()
    + b'"}',
}


class _Stores:
    """One run's store, over bytes a test puts under it, for the in-memory or the local kind."""

    def __init__(self, *, local: bool, root: Path) -> None:
        self._local = local
        self._root = root
        self._contents: bytes | None = None

    def put(self, contents: bytes) -> None:
        """Put these bytes where the run's ``store.json`` is."""
        self._contents = contents
        if self._local:
            seed_store_document(Project.open(self._root), _RUN, contents)

    def store(self, faults: Sequence[ObservationFault | None] = ()) -> StateStore:
        """A store opened over the bytes put so far, as a process that starts later would."""
        if self._local:
            return Project.open(self._root).state_store(_RUN, observation_fault_plan=faults)
        fake = FakeStateStore(observation_fault_plan=faults)
        if self._contents is not None:
            fake.replace_document(self._contents)
        return fake


@pytest.fixture(params=[False, True], ids=["fake", "local"])
def stores(request: pytest.FixtureRequest, tmp_path: Path) -> _Stores:
    return _Stores(local=request.param, root=tmp_path)


def test_a_store_with_no_document_reads_as_none(stores: _Stores) -> None:
    store = stores.store()
    assert store.load() is None
    assert store.observe() is None


def test_a_version_1_document_reads_its_record_and_ignores_its_lease(stores: _Stores) -> None:
    stores.put(_V1_STORE)
    store = stores.store()
    assert store.load() == _V1_RECORD
    assert store.observe() == _V1_RECORD


@given(
    payload=st.binary(max_size=1024),
    revision=st.integers(min_value=0, max_value=5),
    blocked=st.booleans(),
)
@example(payload=b"busy run", revision=0, blocked=False)
@example(payload=b"", revision=3, blocked=True)
def test_a_saved_record_reads_back_whole_from_a_store_opened_again(
    payload: bytes, revision: int, *, blocked: bool
) -> None:
    saved: StoreRecord = (
        QuarantinedEnvelope(
            revision=revision, schema_version=None, payload=payload, reason="blocked"
        )
        if blocked
        else StoredEnvelope(revision=revision, schema_version=1, payload=payload)
    )
    fake = FakeStateStore()
    fake.replace_document(stored_record_bytes(saved))
    with TemporaryDirectory() as directory:
        project = Project.open(Path(directory))
        seed_stored_record(project, _RUN, saved)
        local = Project.open(Path(directory)).state_store(_RUN)
        for store in (fake, local):
            assert store.load() == saved
            assert store.observe() == saved
        assert Project.open(Path(directory)).stored_record(_RUN) == saved


def test_a_run_without_a_record_reads_as_none_and_creates_no_state(tmp_path: Path) -> None:
    assert Project.open(tmp_path).stored_record(_RUN) is None
    assert not (tmp_path / ".vibesys").exists()


@pytest.mark.parametrize("read", ["load", "observe"])
@pytest.mark.parametrize("damage", sorted(_DAMAGES))
def test_a_damaged_document_is_a_typed_error_that_does_not_echo_it(
    stores: _Stores, damage: str, read: str
) -> None:
    stores.put(_DAMAGES[damage])
    with pytest.raises(StateDocumentDamagedError) as failure:
        getattr(stores.store(), read)()
    assert _LEAK_MARKER not in str(failure.value)
    assert "store.json" in str(failure.value)


@pytest.mark.parametrize("read", ["load", "observe"])
def test_a_document_from_a_newer_release_names_both_versions(stores: _Stores, read: str) -> None:
    stores.put(b'{"version":3}')
    with pytest.raises(StateDocumentDamagedError, match=r"version 3.*reads version 2"):
        getattr(stores.store(), read)()


@pytest.mark.parametrize("fault", list(ObservationFault))
def test_a_failed_read_fails_the_one_read_and_leaves_the_record_whole(
    stores: _Stores, fault: ObservationFault
) -> None:
    saved = StoredEnvelope(revision=0, schema_version=1, payload=b"state,outbox,cursor")
    stores.put(stored_record_bytes(saved))
    store = stores.store(faults=(fault,))
    with pytest.raises(OSError, match="state-store"):
        store.load()
    assert store.load() == saved


@pytest.mark.skipif(os.geteuid() == 0, reason="directory permissions require an unprivileged user")
@pytest.mark.parametrize("restrict_before_save", [False, True], ids=["reload", "first-use"])
@given(depth=st.integers(min_value=1, max_value=4), payload=st.binary(max_size=64))
@example(depth=1, payload=b"already-durable")
@settings(max_examples=examples(pr=5, nightly=12))
def test_a_store_below_a_traverse_only_ancestor_is_read(
    *, restrict_before_save: bool, depth: int, payload: bytes
) -> None:
    """Reading a record never needs read access to an ancestor outside the project."""
    with TemporaryDirectory() as directory:
        restricted = Path(directory) / "traverse-only"
        root = restricted.joinpath(*(f"level-{index}" for index in range(depth)), "project")
        root.mkdir(parents=True)
        project = Project.open(root)
        if restrict_before_save:
            restricted.chmod(0o111)
        try:
            saved = StoredEnvelope(revision=0, schema_version=1, payload=payload)
            seed_stored_record(project, _RUN, saved)
            restricted.chmod(0o111)
            with pytest.raises(PermissionError):
                tuple(restricted.iterdir())
            assert Project.open(root).state_store(_RUN).load() == saved
            # A sibling run creates its namespace links while the outer ancestor stays traverse-only.
            assert project.state_store("run-2").load() is None
        finally:
            restricted.chmod(0o700)


@pytest.mark.parametrize(
    ("model", "values", "key"),
    [
        (StoredEnvelope, {"revision": -1, "schema_version": 1, "payload": b""}, "revision"),
        (StoredEnvelope, {"revision": 0, "schema_version": 0, "payload": b""}, "schema_version"),
        (StoredEnvelope, {"revision": 0, "schema_version": 1, "payload": b"", "x": 1}, "x"),
        (
            QuarantinedEnvelope,
            {"revision": 1, "schema_version": 1, "payload": b"", "reason": ""},
            "reason",
        ),
        (StoreFence, {"host_id": "", "epoch": 1, "expires_at": 10}, "host_id"),
        (StoreFence, {"host_id": "host-a", "epoch": 0, "expires_at": 10}, "epoch"),
        (StoreFence, {"host_id": "host-a", "epoch": 1, "expires_at": float("nan")}, "expires_at"),
    ],
)
def test_contract_models_reject_invalid_values_and_unknown_keys(
    model: type[StoredEnvelope | QuarantinedEnvelope | StoreFence],
    values: dict[str, object],
    key: str,
) -> None:
    with pytest.raises(ValidationError, match=key):
        model.model_validate(values)


@pytest.mark.parametrize("invalid", [True, False, 0.0, "0"])
@pytest.mark.parametrize("key", ["revision", "schema_version"])
def test_envelope_integer_metadata_rejects_coercion(invalid: object, key: str) -> None:
    values: dict[str, object] = {"revision": 0, "schema_version": 1, "payload": b""}
    values[key] = invalid
    with pytest.raises(ValidationError, match=key):
        StoredEnvelope.model_validate(values)
