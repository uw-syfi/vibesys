"""The same durable-transition contract applies to disk and memory stores."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_project.api import (
    CommitFault,
    Committed,
    Conflict,
    ConflictReason,
    FakeStateStore,
    Project,
    QuarantinedEnvelope,
    StateStoreWriteError,
    StoredEnvelope,
    StoreFence,
    Unknown,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_project.api import StateStore

    type StoreFactory = Callable[[Path, tuple[CommitFault, ...]], StateStore]


def _fake(_root: Path, faults: tuple[CommitFault, ...] = ()) -> StateStore:
    return FakeStateStore(fault_plan=faults)


def _local(root: Path, faults: tuple[CommitFault, ...] = ()) -> StateStore:
    return Project.open(root).state_store("run-1", fault_plan=faults)


@pytest.fixture(scope="session", params=[_fake, _local], ids=["fake", "local"])
def make_store(request: pytest.FixtureRequest) -> StoreFactory:
    return request.param


def _envelope(revision: int, payload: bytes = b"state,outbox,cursor") -> StoredEnvelope:
    return StoredEnvelope(revision=revision, schema_version=1, payload=payload)


def test_empty_store_and_atomic_revision_conflict(make_store: StoreFactory, tmp_path: Path) -> None:
    store = make_store(tmp_path, ())
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    assert store.load() is None

    envelope = _envelope(0)
    assert store.commit(None, envelope, fence, now=1) == Committed(record=envelope)
    assert store.load() == envelope
    conflict = store.commit(None, _envelope(0, b"other"), fence, now=2)
    assert conflict == Conflict(reason=ConflictReason.REVISION, revision=0)
    assert store.load() == envelope


@given(
    operations=st.lists(st.tuples(st.booleans(), st.binary(max_size=64)), min_size=1, max_size=25)
)
@settings(max_examples=20)
def test_commit_sequences_follow_compare_and_swap(
    make_store: StoreFactory, operations: list[tuple[bool, bytes]]
) -> None:
    with TemporaryDirectory() as directory:
        store = make_store(Path(directory), ())
        fence = store.acquire("host-a", now=0, duration=100)
        assert fence is not None
        latest = None
        revision = None
        for conflict_requested, payload in operations:
            expected = (0 if revision is None else revision + 1) if conflict_requested else revision
            envelope = _envelope(0 if expected is None else expected + 1, payload)
            result = store.commit(expected, envelope, fence, now=1)
            if conflict_requested:
                assert result == Conflict(reason=ConflictReason.REVISION, revision=revision)
            else:
                assert result == Committed(record=envelope)
                latest = envelope
                revision = envelope.revision
            assert store.load() == latest


@pytest.mark.parametrize("fault", list(CommitFault))
@given(payload=st.binary(max_size=128))
def test_write_faults_are_resolved_only_by_reload(
    make_store: StoreFactory, fault: CommitFault, payload: bytes
) -> None:
    with TemporaryDirectory() as directory:
        store = make_store(Path(directory), (fault,))
        fence = store.acquire("host-a", now=0, duration=10)
        assert fence is not None
        envelope = _envelope(0, payload)
        if fault == CommitFault.FAILED:
            with pytest.raises(StateStoreWriteError):
                store.commit(None, envelope, fence, now=1)
        else:
            assert store.commit(None, envelope, fence, now=1) == Unknown(revision=0)
        latest = envelope if fault == CommitFault.UNKNOWN_AFTER else None
        assert store.load() == latest
        revision = 0 if latest is not None else None
        successor = _envelope(0 if revision is None else revision + 1, b"resolved")
        assert store.commit(revision, successor, fence, now=2) == Committed(record=successor)
        assert store.load() == successor


def test_conflict_does_not_consume_fault_plan(make_store: StoreFactory, tmp_path: Path) -> None:
    store = make_store(tmp_path, (CommitFault.UNKNOWN_AFTER,))
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    assert isinstance(store.commit(1, _envelope(2), fence, now=1), Conflict)
    assert store.commit(None, _envelope(0), fence, now=2) == Unknown(revision=0)
    assert store.load() == _envelope(0)


def test_fence_renewal_expiry_and_takeover(make_store: StoreFactory, tmp_path: Path) -> None:
    store = make_store(tmp_path, ())
    first = store.acquire("host-a", now=0, duration=10)
    assert first is not None
    assert store.verify(first, now=0)
    assert store.acquire("host-b", now=9, duration=10) is None
    renewed = store.renew(first, now=9, duration=10)
    assert renewed is not None
    assert renewed.epoch == first.epoch
    assert renewed.expires_at == 19
    assert store.verify(renewed, now=18)
    assert store.verify(first, now=18)
    assert not store.verify(renewed, now=19)
    second = store.acquire("host-b", now=19, duration=10)
    assert second is not None
    assert second.epoch > first.epoch
    assert store.renew(renewed, now=19, duration=10) is None
    assert not store.verify(renewed, now=19)
    assert store.verify(second, now=19)
    assert store.commit(None, _envelope(0), renewed, now=19) == Conflict(
        reason=ConflictReason.FENCE, revision=None
    )
    assert store.load() is None
    assert store.commit(None, _envelope(0), second, now=19) == Committed(record=_envelope(0))


def test_quarantine_is_an_explicit_record_not_a_ready_envelope(
    make_store: StoreFactory, tmp_path: Path
) -> None:
    store = make_store(tmp_path, ())
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    blocked = QuarantinedEnvelope(
        revision=0, schema_version=8, payload=b"legacy", reason="missing ownership proof"
    )
    assert store.quarantine(None, blocked, fence, now=1) == Committed(record=blocked)
    assert store.load() == blocked
    assert not isinstance(store.load(), StoredEnvelope)
    assert isinstance(store.commit(None, _envelope(0), fence, now=2), Conflict)
    resolved = _envelope(1)
    assert store.commit(0, resolved, fence, now=3) == Committed(record=resolved)
    assert store.load() == resolved


def test_quarantine_preserves_an_unknown_source_schema_version(
    make_store: StoreFactory, tmp_path: Path
) -> None:
    store = make_store(tmp_path, ())
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    blocked = QuarantinedEnvelope(
        revision=0, schema_version=None, payload=b"unversioned", reason="missing schema version"
    )
    assert store.quarantine(None, blocked, fence, now=1) == Committed(record=blocked)
    assert store.load() == blocked


def test_quarantine_replaces_ready_record_in_the_same_revision_domain(
    make_store: StoreFactory, tmp_path: Path
) -> None:
    store = make_store(tmp_path, ())
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    ready = _envelope(0)
    assert store.commit(None, ready, fence, now=1) == Committed(record=ready)
    blocked = QuarantinedEnvelope(
        revision=1, schema_version=1, payload=b"unsupported", reason="schema migration failed"
    )
    assert store.quarantine(0, blocked, fence, now=2) == Committed(record=blocked)
    assert store.load() == blocked
    assert isinstance(store.commit(0, _envelope(1), fence, now=3), Conflict)


@given(
    faults=st.lists(st.sampled_from(list(CommitFault)), min_size=1, max_size=12),
    payload=st.binary(max_size=64),
)
@settings(max_examples=20)
def test_fault_histories_preserve_prior_or_complete_successor(
    make_store: StoreFactory, faults: list[CommitFault], payload: bytes
) -> None:
    with TemporaryDirectory() as directory:
        store = make_store(Path(directory), tuple(faults))
        fence = store.acquire("host-a", now=0, duration=100)
        assert fence is not None
        latest = None
        for fault in faults:
            revision = None if latest is None else latest.revision
            envelope = _envelope(0 if revision is None else revision + 1, payload)
            if fault == CommitFault.FAILED:
                with pytest.raises(StateStoreWriteError):
                    store.commit(revision, envelope, fence, now=1)
            else:
                assert store.commit(revision, envelope, fence, now=1) == Unknown(
                    revision=envelope.revision
                )
            if fault == CommitFault.UNKNOWN_AFTER:
                latest = envelope
            assert store.load() == latest


def test_same_host_reacquires_only_after_expiry_with_a_new_epoch(
    make_store: StoreFactory, tmp_path: Path
) -> None:
    store = make_store(tmp_path, ())
    first = store.acquire("host-a", now=0, duration=10)
    assert first is not None
    assert store.acquire("host-a", now=1, duration=10) is None
    assert store.renew(first, now=10, duration=10) is None
    second = store.acquire("host-a", now=10, duration=10)
    assert second is not None
    assert second.epoch > first.epoch
    assert not store.verify(first, now=10)
    assert store.verify(second, now=10)


def test_backward_supplied_time_cannot_restore_fence_authority(
    make_store: StoreFactory, tmp_path: Path
) -> None:
    store = make_store(tmp_path, ())
    first = store.acquire("host-a", now=10, duration=10)
    assert first is not None
    assert not store.verify(first, now=9)
    assert store.renew(first, now=9, duration=10) is None
    assert store.acquire("host-b", now=9, duration=10) is None
    assert store.commit(None, _envelope(0), first, now=9) == Conflict(
        reason=ConflictReason.FENCE, revision=None
    )
    assert store.load() is None


@pytest.mark.parametrize("revision", [1, 2])
def test_commit_rejects_non_successor_revision(
    make_store: StoreFactory, tmp_path: Path, revision: int
) -> None:
    store = make_store(tmp_path, ())
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    with pytest.raises(ValueError, match="revision"):
        store.commit(None, _envelope(revision), fence, now=1)
    assert store.load() is None


def test_competing_commits_publish_exactly_one_whole_record(
    make_store: StoreFactory, tmp_path: Path
) -> None:
    store = make_store(tmp_path, ())
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    barrier = Barrier(4)

    def compete(index: int) -> Committed | Conflict | Unknown:
        barrier.wait()
        return store.commit(None, _envelope(0, bytes([index])), fence, now=1)

    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(compete, range(4)))
    committed = [result for result in results if isinstance(result, Committed)]
    assert len(committed) == 1
    assert store.load() == committed[0].record
    assert sum(isinstance(result, Conflict) for result in results) == 3


@pytest.mark.parametrize("fault", list(CommitFault))
def test_only_published_writes_advance_the_time_watermark(
    make_store: StoreFactory, tmp_path: Path, fault: CommitFault
) -> None:
    store = make_store(tmp_path, (fault,))
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    if fault == CommitFault.FAILED:
        with pytest.raises(StateStoreWriteError):
            store.commit(None, _envelope(0), fence, now=5)
    else:
        assert isinstance(store.commit(None, _envelope(0), fence, now=5), Unknown)
    assert store.verify(fence, now=4) == (fault != CommitFault.UNKNOWN_AFTER)


def test_successful_commit_advances_the_time_watermark(
    make_store: StoreFactory, tmp_path: Path
) -> None:
    store = make_store(tmp_path, ())
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    envelope = _envelope(0)
    assert store.commit(None, envelope, fence, now=5) == Committed(record=envelope)
    assert not store.verify(fence, now=4)
    assert store.commit(0, _envelope(1), fence, now=4) == Conflict(
        reason=ConflictReason.FENCE, revision=0
    )
    assert store.load() == envelope


@pytest.mark.parametrize("fault", [CommitFault.UNKNOWN_BEFORE, CommitFault.UNKNOWN_AFTER])
def test_quarantine_ambiguous_write_reloads_whole_record(
    make_store: StoreFactory, tmp_path: Path, fault: CommitFault
) -> None:
    store = make_store(tmp_path, (fault,))
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    blocked = QuarantinedEnvelope(
        revision=0, schema_version=1, payload=b"opaque", reason="unknown schema"
    )
    assert store.quarantine(None, blocked, fence, now=1) == Unknown(revision=0)
    assert store.load() == (blocked if fault == CommitFault.UNKNOWN_AFTER else None)


@given(payload=st.binary(max_size=128))
def test_local_reopen_preserves_opaque_bytes_and_fence(payload: bytes) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        first = _local(root)
        fence = first.acquire("host-a", now=0, duration=10)
        assert fence is not None
        envelope = _envelope(0, payload)
        assert first.commit(None, envelope, fence, now=1) == Committed(record=envelope)
        reopened = _local(root)
        assert reopened.load() == envelope
        assert reopened.verify(fence, now=2)
        assert reopened.acquire("host-b", now=2, duration=10) is None
        assert isinstance(reopened.commit(None, envelope, fence, now=2), Conflict)


def test_separately_opened_local_stores_share_atomic_cas(tmp_path: Path) -> None:
    first = _local(tmp_path)
    second = _local(tmp_path)
    fence = first.acquire("host-a", now=0, duration=10)
    assert fence is not None
    barrier = Barrier(2)

    def compete(store: StateStore, payload: bytes) -> Committed | Conflict | Unknown:
        barrier.wait()
        return store.commit(None, _envelope(0, payload), fence, now=1)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(compete, first, b"first"),
            workers.submit(compete, second, b"second"),
        ]
        results = [future.result() for future in futures]
    committed = [result for result in results if isinstance(result, Committed)]
    assert len(committed) == 1
    assert sum(isinstance(result, Conflict) for result in results) == 1
    assert first.load() == second.load() == committed[0].record


@pytest.mark.parametrize(
    ("model", "values", "key"),
    [
        (StoredEnvelope, {"revision": -1, "schema_version": 1, "payload": b""}, "revision"),
        (StoredEnvelope, {"revision": 1, "schema_version": 0, "payload": b""}, "schema_version"),
        (
            StoredEnvelope,
            {"revision": 1, "schema_version": 1, "payload": b"", "outbox": []},
            "outbox",
        ),
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


@pytest.mark.parametrize("invalid", [True, False, 0.0, "0"])
def test_expected_revision_rejects_coercion(
    make_store: StoreFactory, tmp_path: Path, invalid: object
) -> None:
    store = make_store(tmp_path, ())
    fence = store.acquire("host-a", now=0, duration=10)
    assert fence is not None
    with pytest.raises(ValueError, match="revision"):
        store.commit(cast("int", invalid), _envelope(1), fence, now=1)
    assert store.load() is None


@pytest.mark.parametrize("invalid", [-1, float("nan"), float("inf"), True, "1"])
def test_supplied_clock_rejects_invalid_values(
    make_store: StoreFactory, tmp_path: Path, invalid: object
) -> None:
    store = make_store(tmp_path, ())
    with pytest.raises(ValueError, match="now"):
        store.acquire("host-a", now=cast("float", invalid), duration=10)
    assert store.load() is None


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf"), True, "1"])
def test_lease_duration_rejects_invalid_values(
    make_store: StoreFactory, tmp_path: Path, invalid: object
) -> None:
    store = make_store(tmp_path, ())
    with pytest.raises(ValueError, match="duration"):
        store.acquire("host-a", now=0, duration=cast("float", invalid))
    assert store.load() is None


@given(duration=st.integers(min_value=1, max_value=1000))
def test_renewal_does_not_shrink_the_persisted_lease(
    make_store: StoreFactory, duration: int
) -> None:
    with TemporaryDirectory() as directory:
        store = make_store(Path(directory), ())
        first = store.acquire("host-a", now=0, duration=duration + 2)
        assert first is not None
        renewed = store.renew(first, now=1, duration=duration)
        assert renewed is not None
        assert renewed.expires_at == first.expires_at
        assert renewed.epoch == first.epoch
        assert store.verify(first, now=duration + 1)
        assert store.acquire("host-b", now=duration + 1, duration=1) is None


@given(
    operations=st.lists(
        st.tuples(st.booleans(), st.binary(max_size=64), st.integers(min_value=1, max_value=20)),
        min_size=1,
        max_size=12,
    )
)
@settings(max_examples=20)
def test_record_and_takeover_histories_preserve_cas_and_fence_authority(
    make_store: StoreFactory, operations: list[tuple[bool, bytes, int]]
) -> None:
    with TemporaryDirectory() as directory:
        store = make_store(Path(directory), ())
        fence = store.acquire("host-a", now=0, duration=1)
        assert fence is not None
        latest = None
        now = 0
        for quarantine, payload, duration in operations:
            expected = None if latest is None else latest.revision
            revision = 0 if expected is None else expected + 1
            if quarantine:
                candidate = QuarantinedEnvelope(
                    revision=revision,
                    schema_version=None,
                    payload=payload,
                    reason="missing reconstruction proof",
                )
                result = store.quarantine(expected, candidate, fence, now=now)
            else:
                candidate = _envelope(revision, payload)
                result = store.commit(expected, candidate, fence, now=now)
            assert result == Committed(record=candidate)
            latest = candidate
            assert store.load() == latest

            prior_fence = fence
            now = fence.expires_at
            host = "host-b" if fence.host_id == "host-a" else "host-a"
            fence = store.acquire(host, now=now, duration=duration)
            assert fence is not None
            assert fence.epoch > prior_fence.epoch
            assert not store.verify(prior_fence, now=now)
            assert store.verify(fence, now=now)
            assert store.commit(
                revision, _envelope(revision + 1), prior_fence, now=now
            ) == Conflict(reason=ConflictReason.FENCE, revision=revision)
            assert store.load() == latest


@pytest.mark.parametrize("fault", [CommitFault.UNKNOWN_BEFORE, CommitFault.UNKNOWN_AFTER])
@given(payload=st.binary(max_size=64))
def test_ambiguous_reload_after_takeover_does_not_restore_old_host_authority(
    make_store: StoreFactory, fault: CommitFault, payload: bytes
) -> None:
    with TemporaryDirectory() as directory:
        store = make_store(Path(directory), (fault,))
        first = store.acquire("host-a", now=0, duration=2)
        assert first is not None
        envelope = _envelope(0, payload)
        assert store.commit(None, envelope, first, now=1) == Unknown(revision=0)
        second = store.acquire("host-b", now=2, duration=2)
        assert second is not None
        latest = store.load()
        assert latest == (envelope if fault == CommitFault.UNKNOWN_AFTER else None)
        assert not store.verify(first, now=2)
        assert store.renew(first, now=2, duration=2) is None
        revision = None if latest is None else latest.revision
        successor = _envelope(0 if revision is None else revision + 1, b"reconciled")
        assert store.commit(revision, successor, first, now=2) == Conflict(
            reason=ConflictReason.FENCE, revision=revision
        )
        assert store.load() == latest
        assert store.commit(revision, successor, second, now=2) == Committed(record=successor)
        assert store.load() == successor
