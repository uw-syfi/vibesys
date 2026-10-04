"""A durable store never requires read access to ancestors outside its project."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_project.api import Committed, Project, StoredEnvelope


@pytest.mark.skipif(os.geteuid() == 0, reason="directory permissions require an unprivileged user")
@pytest.mark.parametrize("restrict_before_write", [False, True], ids=["reload", "first-use"])
@given(depth=st.integers(min_value=1, max_value=4), payload=st.binary(max_size=64))
@example(depth=1, payload=b"already-durable")
@settings(max_examples=12)
def test_store_operations_below_a_traverse_only_ancestor(
    *, restrict_before_write: bool, depth: int, payload: bytes
) -> None:
    with TemporaryDirectory() as directory:
        restricted = Path(directory) / "traverse-only"
        root = restricted.joinpath(*(f"level-{index}" for index in range(depth)), "project")
        root.mkdir(parents=True)
        project = Project.open(root)
        store = project.state_store("run-1")
        if restrict_before_write:
            restricted.chmod(0o111)
        try:
            fence = store.acquire("host-a", now=0, duration=10)
            assert fence is not None
            envelope = StoredEnvelope(revision=0, schema_version=1, payload=payload)
            assert store.commit(None, envelope, fence, now=1) == Committed(record=envelope)
            restricted.chmod(0o111)
            with pytest.raises(PermissionError):
                tuple(restricted.iterdir())
            namespace = project.state.state_store_namespace("run-1")
            assert namespace.read_bytes("store.json") is not None

            reopened = Project.open(root).state_store("run-1")
            assert reopened.load() == envelope
            assert reopened.verify(fence, now=2)
            renewed = reopened.renew(fence, now=2, duration=20)
            assert renewed is not None
            successor = StoredEnvelope(revision=1, schema_version=1, payload=payload + b"next")
            assert reopened.commit(0, successor, renewed, now=3) == Committed(record=successor)
            assert store.load() == successor

            # A sibling run creates namespace links while the outer ancestor
            # remains traverse-only, and must persist them inside the project.
            sibling = project.state_store("run-2")
            assert sibling.acquire("host-b", now=0, duration=10) is not None
            assert sibling.load() is None
        finally:
            restricted.chmod(0o700)
