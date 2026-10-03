"""Properties of immutable content-addressed artifacts through the public API."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
)
from vs_runtime.api import (
    ArtifactCorruptionError,
    ArtifactReceipt,
    ArtifactStore,
    ArtifactStoreError,
)


@pytest.fixture(autouse=True)
def _state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path.parent / f".state-{tmp_path.name}"))


def _store(root: Path, run_id: str = "run-1") -> ArtifactStore:
    project = Project.open(root)
    now = datetime(2026, 10, 3, tzinfo=UTC)
    project.state.create_project("artifacts", now=now)
    project.state.create_run(
        project.state.new_run_manifest(
            "artifacts",
            branch="artifacts",
            run_id=run_id,
            vibesys_version="0.1.0",
            trusted_input_baseline="a" * 40,
            now=now,
            run_environment=RunEnvironmentRecord(name="local"),
            execution=RunExecutionRecord(
                model="test-model",
                agent_backend="test-backend",
                compute_backend="local",
                requested_profiler="none",
                resolved_profiler="none",
                agent_roles={},
            ),
            orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
        )
    )
    return ArtifactStore(project.state.portable_namespace(run_id, "results"))


@given(content=st.binary(max_size=8192))
def test_write_is_idempotent_and_read_verifies_content(content: bytes) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = _store(root)
        first = store.write(content)
        original_inode = first.path.stat().st_ino
        second = _store(root).write(content)
        assert first.path.stat().st_ino == original_inode
        assert first == second
        assert first.path.is_absolute()
        assert first.path.name == first.sha256 == hashlib.sha256(content).hexdigest()
        assert first.size == len(content)
        assert store.read(first) == content
        assert _store(root).read(first) == content
        assert tuple(first.path.parent.iterdir()) == (first.path,)


@given(content=st.binary(max_size=8192), changed=st.binary(max_size=8192))
def test_corruption_never_returns_unverified_bytes(content: bytes, changed: bytes) -> None:
    if content == changed:
        changed += b"\x00"
    with TemporaryDirectory() as directory:
        store = _store(Path(directory))
        receipt = store.write(content)
        receipt.path.write_bytes(changed)
        with pytest.raises(ArtifactCorruptionError, match="SHA-256"):
            store.read(receipt)
        with pytest.raises(ArtifactCorruptionError, match="SHA-256"):
            store.write(content)
        assert receipt.path.read_bytes() == changed


@given(
    content=st.binary(min_size=1, max_size=8192),
    index=st.integers(min_value=0, max_value=8191),
    bit=st.integers(min_value=0, max_value=7),
)
def test_same_size_corruption_requires_hash_verification(
    content: bytes, index: int, bit: int
) -> None:
    changed = bytearray(content)
    changed[index % len(content)] ^= 1 << bit
    corrupted = bytes(changed)
    with TemporaryDirectory() as directory:
        store = _store(Path(directory))
        receipt = store.write(content)
        receipt.path.write_bytes(corrupted)
        assert receipt.path.stat().st_size == receipt.size
        with pytest.raises(ArtifactCorruptionError, match="SHA-256"):
            store.read(receipt)
        with pytest.raises(ArtifactCorruptionError, match="SHA-256"):
            store.write(content)
        assert receipt.path.read_bytes() == corrupted


def test_receipts_require_a_successful_write(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match=r"only by ArtifactStore\.write"):
        ArtifactReceipt()
    with pytest.raises(TypeError, match="must be bytes"):
        _store(tmp_path).write(cast("bytes", "unencoded"))


def test_missing_receipt_file_is_a_typed_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    receipt = store.write(b"contents")
    receipt.path.unlink()
    with pytest.raises(ArtifactStoreError, match=receipt.sha256):
        store.read(receipt)


def test_receipt_cannot_read_a_different_store(tmp_path: Path) -> None:
    receipt = _store(tmp_path).write(b"contents")
    with pytest.raises(ArtifactStoreError, match="another store"):
        _store(tmp_path, "run-2").read(receipt)


def test_symlink_is_corruption_and_is_never_followed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    receipt = store.write(b"contents")
    other = tmp_path / "other"
    other.write_bytes(b"contents")
    receipt.path.unlink()
    receipt.path.symlink_to(other)
    with pytest.raises(ArtifactCorruptionError, match="symlink"):
        store.read(receipt)
    with pytest.raises(ArtifactCorruptionError, match="symlink"):
        store.write(b"contents")


def test_concurrent_writes_return_the_same_verified_receipt(tmp_path: Path) -> None:
    stores = [_store(tmp_path) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = tuple(executor.map(lambda store: store.write(b"shared"), stores))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert stores[0].read(receipts[0]) == b"shared"
    assert tuple(receipts[0].path.parent.iterdir()) == (receipts[0].path,)
