"""A run's sweeps know a withdrawal mark an earlier sweep of the same process left for their own.

A sweep's mark write can land while its answer is lost, and the read and the
undo that follow can fail too: the mark stays. The next sweep of the same
process must take it up and finish the withdrawal; a sweep that reads it as
another withdrawer's leaves it standing, and every run that asks for the
request waits out the mark's whole window (830 s at the default transport
timeout).
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from vs_sandbox.api.slurm import SlurmLedgerAttachment
from vs_sim.api.testing import ManualClock
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitted,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerEntry,
    LedgerError,
    LedgerPolicy,
    LedgerRecord,
    LedgerState,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmJobStatus,
    SlurmSshTransport,
    WithdrawalMark,
    WithdrawalUndone,
    Withdrawn,
    WithdrawUnknown,
    evaluation_key,
    job_of_handle,
    ledger_operation_id,
)

PROJECT = "demo-0123456789ab"
CONFIG = SlurmConfig(
    name="fake-cluster", remote_workspace_root="/runs", transport=SlurmSshTransport(host="h")
)
POLICY = LedgerPolicy()
NOW = 100_000.0


class _LosesTheFirstMark(FakeEvaluationLedger):
    """The Fake ledger, where the first mark lands but its answer is lost, and the next reads fail."""

    lost: bool = False
    failing_reads: int = 0

    def read(self, key: str) -> LedgerEntry | None:
        if self.failing_reads:
            self.failing_reads -= 1
            raise LedgerError.transport(ConnectionResetError("ssh: connection reset"))
        return super().read(key)

    def write_withdrawal(
        self, current: LedgerEntry, document: WithdrawalMark | WithdrawalUndone
    ) -> bool:
        won = super().write_withdrawal(current, document)
        if isinstance(document, WithdrawalMark) and not self.lost:
            self.lost = True
            # The withdrawal's next read, and the read of the undo after it, fail too.
            self.failing_reads = 2
            raise LedgerError.transport(ConnectionResetError("ssh: connection reset"))
        return won


@pytest.mark.xfail(strict=True, reason="r3173-recheck LOW-3: each sweep had its own writer id")
def test_a_sweep_takes_up_the_mark_an_earlier_sweep_of_its_process_left_standing() -> None:
    """r3173-recheck LOW-3: a sweep built a new withdrawer, so a new writer id, for each key and pass."""
    with TemporaryDirectory() as directory:
        workspace = Path(directory)
        clock = ManualClock(NOW)
        cluster = FakeCluster(clock=clock)
        ledger = _LosesTheFirstMark(project=PROJECT)
        request = SlurmBatchRequest(
            workspace=workspace, stages=(SlurmBatchStage(name="bench", command=("true",)),)
        )
        key = evaluation_key(PROJECT, request, CONFIG, setup_script_sha256=None)
        operation = ledger_operation_id(key, 0)
        cluster.script(operation, states=(SlurmJobStatus.RUNNING,))
        submitted = cluster.submit(request, operation_id=operation)
        assert isinstance(submitted, ClusterSubmitted)
        assert isinstance(submitted.handle, SlurmBatchHandle)
        # Submitted long ago by a run that is gone: no lease for far longer than the grace.
        ledger.claim(
            LedgerRecord(
                key=key,
                project=PROJECT,
                attempt=0,
                operation_id=operation,
                owner="run-gone",
                created_at=NOW - 10 * POLICY.grace_seconds,
                claim_expires_at=NOW - 10 * POLICY.grace_seconds + POLICY.claim_seconds,
                state=LedgerState.SUBMITTED,
                job=job_of_handle(submitted.handle),
            )
        )
        sweeper = SlurmLedgerAttachment(
            ledger=ledger,
            config=CONFIG,
            project=PROJECT,
            holder="run-sweeper",
            policy=POLICY,
            clock=clock,
        )

        first = sweeper.sweep_if_due(cluster)
        entry = ledger.read(key)
        assert entry is not None
        assert entry.withdrawal is not None  # the lost mark stands
        assert any(isinstance(outcome, WithdrawUnknown) for outcome in first)

        clock.advance(POLICY.lease_seconds)
        second = sweeper.sweep_if_due(cluster)

        assert [type(outcome) for outcome in second] == [Withdrawn]
        entry = ledger.read(key)
        assert entry is not None
        assert entry.record.state is LedgerState.WITHDRAWN
        observed = cluster.inspect(operation)
        assert isinstance(observed, ClusterObservation)
        assert observed.status is SlurmJobStatus.CANCELLED
