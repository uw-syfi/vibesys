"""A run's sweeps know a withdrawal mark an earlier sweep of the same process left for their own.

A sweep's mark write can land while its answer is lost, and the read and the
undo that follow can fail too: the mark stays. The next sweep of the same
process must take it up and finish the withdrawal; a sweep that reads it as
another withdrawer's leaves it standing, and every run that asks for the
request waits out the mark's whole window (830 s at the default transport
timeout).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from vs_sandbox.api.slurm import SlurmLedgerAttachment
from vs_sim.api.testing import ManualClock
from vs_slurm.api import (
    AttachJob,
    AwaitWithdrawal,
    CheckJob,
    ClusterCall,
    ClusterObservation,
    ClusterSubmitted,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerEntry,
    LedgerError,
    LedgerLease,
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
    submit_step,
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


def test_a_sweep_takes_up_the_mark_an_earlier_sweep_of_its_process_left_standing() -> None:
    """r3173-recheck LOW-3: a sweep built a new withdrawer, so a new writer id, for each key and pass."""
    with TemporaryDirectory() as directory:
        workspace = Path(directory)
        clock = ManualClock(NOW)
        cluster = FakeCluster(clock=clock)
        ledger = _LosesTheFirstMark(project=PROJECT, clock=ManualClock())
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


class _LeasedAfterTheNextRead(FakeEvaluationLedger):
    """The Fake ledger, where run-2 leases ``key`` just after the next read of it is answered."""

    key: str = ""
    armed: bool = False

    def read(self, key: str) -> LedgerEntry | None:
        entry = super().read(key)
        if self.armed and key == self.key:
            self.armed = False
            self.renew(key, LedgerLease(holder="run-2", renewed_at=0.0, expires_at=1e12))
        return entry


@dataclass
class _Interleaving:
    """While the first sweep cancels, a later heartbeat of the run comes, then run-2 asks."""

    ledger: _LeasedAfterTheNextRead
    sweeper: SlurmLedgerAttachment
    cluster: FakeCluster
    clock: ManualClock
    key: str
    other: tuple[str, str]
    fired: bool = False
    attached: bool = False
    violations: list[str] = field(default_factory=list)

    def __call__(self, kind: ClusterCall) -> None:
        if kind is ClusterCall.SCANCEL and self.attached:
            self.violations.append("the sweep's scancel killed the job run-2 attached to")
        entry = self.ledger.reopen().read(self.key)
        if self.fired or entry is None or entry.withdrawal is None:
            return  # only once the first sweep's withdrawal of ``key`` is under way
        self.fired = True
        # Another evaluation's heartbeat, a lease period later: a sweep is due again.
        self.clock.advance(POLICY.lease_seconds)
        self.ledger.armed = True  # run-2 leases between that sweep's reads of ``key``
        self.sweeper.heartbeat(self.cluster, *self.other)
        # run-2 asks for the request, holding its lease.
        ledger = self.ledger.reopen()
        self.ledger.armed = False
        ledger.renew(self.key, LedgerLease(holder="run-2", renewed_at=0.0, expires_at=1e12))
        asked = ledger.read(self.key)
        assert asked is not None
        step = submit_step(asked, now=self.clock.now())
        self.attached = isinstance(step, AttachJob | CheckJob)
        assert self.attached or isinstance(step, AwaitWithdrawal)


def _submitted(
    cluster: FakeCluster, ledger: FakeEvaluationLedger, workspace: Path, name: str, owner: str
) -> tuple[str, str]:
    """A request whose job runs, recorded as ``owner`` submitted it long ago; its key and attempt."""
    request = SlurmBatchRequest(
        workspace=workspace, stages=(SlurmBatchStage(name=name, command=("true",)),)
    )
    key = evaluation_key(PROJECT, request, CONFIG, setup_script_sha256=None)
    operation = ledger_operation_id(key, 0)
    cluster.script(operation, states=(SlurmJobStatus.RUNNING,))
    submitted = cluster.submit(request, operation_id=operation)
    assert isinstance(submitted, ClusterSubmitted)
    assert isinstance(submitted.handle, SlurmBatchHandle)
    ledger.claim(
        LedgerRecord(
            key=key,
            project=PROJECT,
            attempt=0,
            operation_id=operation,
            owner=owner,
            created_at=NOW - 10 * POLICY.grace_seconds,
            claim_expires_at=NOW - 10 * POLICY.grace_seconds + POLICY.claim_seconds,
            state=LedgerState.SUBMITTED,
            job=job_of_handle(submitted.handle),
        )
    )
    return key, operation


def test_a_heartbeat_during_a_sweep_starts_no_second_sweep_of_the_process() -> None:
    """r3206 LOW-D: a later heartbeat's sweep undid the first sweep's mark as its own.

    The first sweep marks an unleased request and inspects its job. A heartbeat
    of another evaluation comes a lease period later, so a sweep is due again;
    run-2 leases the request between that second sweep's reads. Sharing the
    first sweep's writer id, the second sweep took the mark for its own, found
    the request leased and undid it; run-2 then attached, and the first sweep's
    scancel killed its job.
    """
    with TemporaryDirectory() as directory:
        workspace = Path(directory)
        clock = ManualClock(NOW)
        ledger = _LeasedAfterTheNextRead(project=PROJECT, clock=ManualClock())
        hooks: list[_Interleaving] = []

        def on_call(kind: ClusterCall) -> None:
            hooks[0](kind)

        cluster = FakeCluster(clock=clock, on_call=on_call)
        sweeper = SlurmLedgerAttachment(
            ledger=ledger,
            config=CONFIG,
            project=PROJECT,
            holder="run-sweeper",
            policy=POLICY,
            clock=clock,
        )
        key, _ = _submitted(cluster, ledger, workspace, "unleased", "run-gone")
        mine = [_submitted(cluster, ledger, workspace, name, "run-sweeper") for name in ("a", "b")]
        for leased, _ in mine:
            sweeper.renew(leased)
        ledger.key = key
        hooks.append(_Interleaving(ledger, sweeper, cluster, clock, key, mine[1]))

        sweeper.heartbeat(cluster, *mine[0])

        assert hooks[0].fired
        assert hooks[0].violations == []


class _ListingFailsOnce(FakeEvaluationLedger):
    """The Fake ledger, where the first listing of the project's requests fails."""

    failed: bool = False

    def list_keys(self) -> tuple[str, ...]:
        if not self.failed:
            self.failed = True
            raise LedgerError.transport(ConnectionResetError("ssh: connection reset"))
        return super().list_keys()


def test_a_sweep_that_raises_leaves_the_next_due_sweep_to_run() -> None:
    """r3223 LOW-4: a sweep's turn is released on every exit, an exception's too.

    Were the claim kept, every later sweep of the process would return at once,
    and no unleased request of the project would ever be withdrawn by it.
    """
    with TemporaryDirectory() as directory:
        clock = ManualClock(NOW)
        cluster = FakeCluster(clock=clock)
        ledger = _ListingFailsOnce(project=PROJECT, clock=clock)
        key, operation = _submitted(cluster, ledger, Path(directory), "unleased", "run-gone")
        sweeper = SlurmLedgerAttachment(
            ledger=ledger,
            config=CONFIG,
            project=PROJECT,
            holder="run-sweeper",
            policy=POLICY,
            clock=clock,
        )

        with pytest.raises(LedgerError):
            sweeper.sweep_if_due(cluster)
        clock.advance(POLICY.lease_seconds)
        swept = sweeper.sweep_if_due(cluster)

        assert [type(outcome) for outcome in swept] == [Withdrawn]
        entry = ledger.read(key)
        assert entry is not None
        assert entry.record.state is LedgerState.WITHDRAWN
        observed = cluster.inspect(operation)
        assert isinstance(observed, ClusterObservation)
        assert observed.status is SlurmJobStatus.CANCELLED
