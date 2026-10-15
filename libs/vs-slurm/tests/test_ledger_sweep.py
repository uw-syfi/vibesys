"""The sweep against other processes' writes that land between its reads and its writes."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest

from vs_sim.api.testing import ManualClock
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitted,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerEntry,
    LedgerLease,
    LedgerRecord,
    LedgerState,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmJobStatus,
    SlurmSshTransport,
    evaluation_key,
    job_of_handle,
    ledger_operation_id,
    sweep_unleased,
    transitioned,
)

if TYPE_CHECKING:
    from collections.abc import Callable

PROJECT = "demo-0123456789ab"
CONFIG = SlurmConfig(
    name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="h")
)
NOW = 1e6


class _Interleaved:
    """The Fake ledger, with another process's write landing just after one chosen read.

    It is the Fake itself, reached through the ``EvaluationLedger`` protocol;
    ``race`` runs once, right after this process's read number ``after_read``
    (1 is the first) returns, as if the other process wrote at that moment.
    """

    def __init__(
        self, inner: FakeEvaluationLedger, *, after_read: int, race: Callable[[], None]
    ) -> None:
        self._inner = inner
        self._after_read = after_read
        self._race = race
        self._reads = 0

    def read(self, key: str) -> LedgerEntry | None:
        entry = self._inner.read(key)
        self._reads += 1
        if self._reads == self._after_read:
            self._race()
        return entry

    def claim(self, record: LedgerRecord) -> LedgerRecord:
        return self._inner.claim(record)

    def replace(self, current: LedgerEntry, record: LedgerRecord) -> bool:
        return self._inner.replace(current, record)

    def renew(self, key: str, lease: LedgerLease) -> None:
        self._inner.renew(key, lease)

    def release(self, key: str, holder: str) -> None:
        self._inner.release(key, holder)

    def list_keys(self) -> tuple[str, ...]:
        return self._inner.list_keys()

    def operator_file_sha256(self, path: str) -> str | None:
        return self._inner.operator_file_sha256(path)


class _Unleased:
    """One request whose job runs and whose only lease expired long before ``NOW``."""

    def __init__(self, root: Path) -> None:
        self.cluster = FakeCluster(clock=ManualClock())
        self.ledger = FakeEvaluationLedger(project=PROJECT)
        workspace = root / "workspace"
        workspace.mkdir()
        request = SlurmBatchRequest(
            workspace=workspace, stages=(SlurmBatchStage(name="bench", command=("true",)),)
        )
        self.key = evaluation_key(PROJECT, request, CONFIG, setup_script_sha256=None)
        self.operation = ledger_operation_id(self.key, 0)
        self.cluster.script(self.operation, states=(SlurmJobStatus.RUNNING,))
        submitted = self.cluster.submit(request, operation_id=self.operation)
        assert isinstance(submitted, ClusterSubmitted)
        assert isinstance(submitted.handle, SlurmBatchHandle)
        self.ledger.claim(
            LedgerRecord(
                key=self.key,
                project=PROJECT,
                attempt=0,
                operation_id=self.operation,
                owner="run-a",
                created_at=0.0,
                claim_expires_at=900.0,
                state=LedgerState.SUBMITTED,
                job=job_of_handle(submitted.handle),
            )
        )
        self.ledger.renew(self.key, LedgerLease(holder="run-a", renewed_at=0.0, expires_at=300.0))

    def attach(self) -> None:
        """Another run attaches to the request: it writes its lease."""
        lease = LedgerLease(holder="run-b", renewed_at=NOW, expires_at=NOW + 300.0)
        self.ledger.reopen().renew(self.key, lease)

    def complete(self) -> None:
        """Another run collected the job's whole result and settles the attempt completed."""
        other = self.ledger.reopen()
        entry = other.read(self.key)
        assert entry is not None
        assert other.replace(entry, transitioned(entry.record, LedgerState.COMPLETED))

    def state(self) -> LedgerState:
        entry = self.ledger.read(self.key)
        assert entry is not None
        return entry.record.state

    def job_status(self) -> SlurmJobStatus:
        observed = self.cluster.inspect(self.operation)
        assert isinstance(observed, ClusterObservation)
        return observed.status


# The sweep reads the request once (read 1) and decides; ``withdraw`` then reads it
# again (read 2) before it cancels, and once more after a lost swap (read 3).
# A lease written after read 2 lands after the check that precedes the cancel, which
# no reader can see; a completion then still loses nothing, since the swap fails.
# Every interleaving of the two writes with the sweep's reads that a reader can see.
_RACES = (("attach", 1), ("complete", 1), ("complete", 2), ("attach_and_complete", 2))


@pytest.mark.parametrize("race", _RACES, ids=[f"{kind}-after-read-{n}" for kind, n in _RACES])
def test_the_sweep_never_withdraws_a_request_wanted_again_after_it_decided(
    race: tuple[str, int],
) -> None:
    """A run that leases the request, or settles it completed, after the sweep's read wins.

    The sweep decided from a read in which no run leased the request. Each
    withdrawal round tests that again on its own read, so a lease written
    meanwhile keeps the job running, and a result collected meanwhile is never
    withdrawn by the retry that follows the lost swap.
    """
    kind, after_read = race
    with TemporaryDirectory() as directory:
        world = _Unleased(Path(directory))

        def interfere() -> None:
            if kind in {"attach", "attach_and_complete"}:
                world.attach()
            if kind in {"complete", "attach_and_complete"}:
                world.complete()

        racing = _Interleaved(world.ledger, after_read=after_read, race=interfere)
        outcomes = sweep_unleased(racing, world.cluster, now=NOW, grace_seconds=0.0)

        assert outcomes == ()
        if kind == "attach":
            assert world.state() is LedgerState.SUBMITTED
            assert world.job_status() is SlurmJobStatus.RUNNING
        else:
            assert world.state() is LedgerState.COMPLETED
        if after_read == 1:
            assert world.job_status() is SlurmJobStatus.RUNNING
