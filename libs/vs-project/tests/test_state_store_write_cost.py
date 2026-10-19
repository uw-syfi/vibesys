"""What a lease operation writes does not depend on the size of the run's record.

A run renews its lease every third of the lease duration. When the lease lived in the same
document as the run's record, each renewal rewrote the whole record, so a long run (a record of
tens of megabytes) spent seconds of its event loop on every renewal (FINDINGS S171). The bytes
each operation publishes are counted at the rename that publishes them, so the check is exact.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_project.api import Committed, LocalAtomicWriteEffects, Project, StoredEnvelope
from vs_sim.api.testing import examples

_LEASE_BYTES = 1024
"""More than a lease document needs: an owner, an epoch, an expiry and a time watermark."""


class _PublishedBytes(LocalAtomicWriteEffects):
    """Local effects that add up the size of every file they publish."""

    def __init__(self) -> None:
        self.published = 0

    def replace(self, temporary: Path, destination: Path) -> None:
        self.published += temporary.stat().st_size
        super().replace(temporary, destination)


@given(size=st.sampled_from([1, 1_000, 100_000, 1_000_000]), renewals=st.integers(1, 4))
@settings(max_examples=examples(pr=4, nightly=12), deadline=None)
def test_a_renewal_publishes_a_few_hundred_bytes_whatever_the_record_size(
    size: int, renewals: int
) -> None:
    with TemporaryDirectory() as directory:
        effects = _PublishedBytes()
        store = Project.open(Path(directory)).state_store("run-1", effects=effects)
        fence = store.acquire("host-a", now=0, duration=60)
        assert fence is not None
        envelope = StoredEnvelope(revision=0, schema_version=1, payload=b"x" * size)
        assert store.commit(None, envelope, fence, now=1) == Committed(record=envelope)
        for renewal in range(renewals):
            before = effects.published
            renewed = store.renew(fence, now=2 + renewal, duration=60)
            assert renewed is not None
            assert effects.published - before <= _LEASE_BYTES, (size, effects.published - before)
        assert store.load() == envelope


@given(size=st.sampled_from([1, 1_000, 100_000, 1_000_000]))
@settings(max_examples=examples(pr=4, nightly=8), deadline=None)
def test_a_commit_publishes_its_record_and_one_lease_document(size: int) -> None:
    with TemporaryDirectory() as directory:
        effects = _PublishedBytes()
        store = Project.open(Path(directory)).state_store("run-1", effects=effects)
        fence = store.acquire("host-a", now=0, duration=60)
        assert fence is not None
        envelope = StoredEnvelope(revision=0, schema_version=1, payload=b"x" * size)
        before = effects.published
        assert store.commit(None, envelope, fence, now=1) == Committed(record=envelope)
        # The payload is stored in base64, four bytes for every three.
        assert effects.published - before <= 4 * size // 3 + 4 + 2 * _LEASE_BYTES
