"""A run that stops wanting a request cancels its job exactly when no other run still wants it."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    WITHDRAWAL_WINDOW_SECONDS,
    ClusterObservation,
    ClusterSubmitted,
    ClusterUnknown,
    FakeCluster,
    FakeEvaluationLedger,
    Kept,
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
    WithdrawalMark,
    WithdrawalUndone,
    Withdrawer,
    Withdrawn,
    WithdrawUnknown,
    evaluation_key,
    job_of_handle,
    ledger_operation_id,
    relinquish,
    withdraw,
)

if TYPE_CHECKING:
    from vs_slurm.api import ClusterCancelOutcome, ClusterTarget

PROJECT = "demo-0123456789ab"
CONFIG = SlurmConfig(
    name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="h")
)
NOW = 1e6
GRACE_S = 1800.0
SELF = "run-self"
OTHERS = ("run-a", "run-b")

# When each other run's lease expires, relative to now; None: it holds no lease.
_expiry = st.none() | st.sampled_from([-GRACE_S - 1.0, -GRACE_S, -1.0, 0.0, 300.0])


def _submitted(
    root: Path, owner: str, ledger: FakeEvaluationLedger | None = None
) -> tuple[FakeEvaluationLedger, FakeCluster, str, str]:
    """A request whose job is queued, claimed by ``owner`` and leased by this run."""
    cluster = FakeCluster(clock=ManualClock(NOW))
    ledger = ledger if ledger is not None else FakeEvaluationLedger(project=PROJECT)
    workspace = root / "workspace"
    workspace.mkdir()
    request = SlurmBatchRequest(
        workspace=workspace, stages=(SlurmBatchStage(name="bench", command=("true",)),)
    )
    key = evaluation_key(PROJECT, request, CONFIG, setup_script_sha256=None)
    operation = ledger_operation_id(key, 0)
    cluster.script(operation, states=(SlurmJobStatus.PENDING,))
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
            created_at=NOW - 10.0,
            claim_expires_at=NOW + 890.0,
            state=LedgerState.SUBMITTED,
            job=job_of_handle(submitted.handle),
        )
    )
    ledger.renew(key, LedgerLease(holder=SELF, renewed_at=NOW, expires_at=NOW + 300.0))
    return ledger, cluster, key, operation


@settings(max_examples=examples(pr=60, nightly=600))
@given(
    owner=st.sampled_from([SELF, *OTHERS]),
    expiries=st.tuples(*(_expiry for _ in OTHERS)),
)
# The run that claimed the request let go of it first: its claim alone keeps nothing.
@example(owner="run-a", expiries=(None, None))
# A lease that lapsed exactly one grace period ago still holds the request.
@example(owner=SELF, expiries=(-GRACE_S, None))
def test_relinquish_cancels_the_job_iff_no_other_run_leased_it_within_the_grace(
    owner: str, expiries: tuple[float | None, ...]
) -> None:
    with TemporaryDirectory() as directory:
        ledger, cluster, key, operation = _submitted(Path(directory), owner)
        for holder, expiry in zip(OTHERS, expiries, strict=True):
            if expiry is not None:
                lease = LedgerLease(holder=holder, renewed_at=NOW, expires_at=NOW + expiry)
                ledger.renew(key, lease)
        wanted = any(expiry is not None and expiry >= -GRACE_S for expiry in expiries)

        stopping = Withdrawer(by=SELF, now=NOW, reason=f"its run {SELF} stopped wanting it")
        outcome = relinquish(ledger, cluster, key, stopping, since=NOW - GRACE_S)

        entry = ledger.read(key)
        assert entry is not None
        assert SELF not in {lease.holder for lease in entry.leases}
        observed = cluster.inspect(operation)
        assert isinstance(observed, ClusterObservation)
        if wanted:
            assert isinstance(outcome, Kept)
            assert entry.record.state is LedgerState.SUBMITTED
            assert not observed.status.terminal
        else:
            assert isinstance(outcome, Withdrawn)
            assert entry.record.state is LedgerState.WITHDRAWN
            assert observed.status is SlurmJobStatus.CANCELLED


class _CancelUnreachable(FakeCluster):
    """The Fake cluster, whose every scancel is lost on the way: the caller hears nothing."""

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        named = target if isinstance(target, str) and not by_job_id else None
        return ClusterUnknown(operation_id=named, reason="ssh: connection reset")


def _withdrawer(by: str, *, now: float = NOW) -> Withdrawer:
    return Withdrawer(by=by, now=now, reason=f"{by} withdrew it")


def _mark(ledger: FakeEvaluationLedger, key: str, by: str, *, since: float) -> WithdrawalMark:
    """Mark the request's attempt by ``by`` from ``since``, as a withdrawer that then died."""
    entry = ledger.read(key)
    assert entry is not None
    mark = WithdrawalMark(
        attempt=entry.record.attempt,
        by=by,
        since=since,
        until=since + WITHDRAWAL_WINDOW_SECONDS,
        reason="gone",
    )
    assert ledger.write_withdrawal(entry, mark)
    return mark


def test_a_withdrawal_whose_scancel_is_unknown_undoes_its_mark() -> None:
    with TemporaryDirectory() as directory:
        ledger, _cluster, key, operation = _submitted(Path(directory), SELF)
        before = ledger.read(key)
        assert before is not None
        unreachable = _CancelUnreachable(clock=ManualClock(NOW))

        outcome = withdraw(ledger, unreachable, key, _withdrawer(SELF))

        assert isinstance(outcome, WithdrawUnknown)
        after = ledger.read(key)
        assert after is not None
        assert after.record == before.record  # SUBMITTED, with its job
        assert after.revision == before.revision
        assert after.withdrawal is None  # undone: a run may attach again
        assert after.record.operation_id == operation


def test_another_withdrawal_under_way_is_waited_for_until_its_window_ends() -> None:
    """A live mark is another withdrawer's to finish; a lapsed one is undone and decided afresh."""
    with TemporaryDirectory() as directory:
        ledger, cluster, key, operation = _submitted(Path(directory), SELF)
        mark = _mark(ledger, key, "run-dead", since=NOW)

        during = withdraw(ledger, cluster, key, _withdrawer(SELF, now=NOW + 1.0))
        assert isinstance(during, WithdrawUnknown)
        assert "run-dead" in during.reason
        entry = ledger.read(key)
        assert entry is not None
        assert entry.withdrawal == mark
        observed = cluster.inspect(operation)
        assert isinstance(observed, ClusterObservation)
        assert not observed.status.terminal

        late = NOW + WITHDRAWAL_WINDOW_SECONDS + 1.0
        after = withdraw(ledger, cluster, key, _withdrawer(SELF, now=late))
        assert isinstance(after, Withdrawn)
        entry = ledger.read(key)
        assert entry is not None
        assert entry.record.state is LedgerState.WITHDRAWN
        assert entry.record.reason == f"{SELF} withdrew it"


class _LeasedOnceMarked(FakeEvaluationLedger):
    """The Fake ledger, where another run writes its lease just after a withdrawal's mark."""

    lease: LedgerLease | None = None

    def write_withdrawal(
        self, current: LedgerEntry, document: WithdrawalMark | WithdrawalUndone
    ) -> bool:
        won = super().write_withdrawal(current, document)
        if won and isinstance(document, WithdrawalMark) and self.lease is not None:
            self.renew(current.record.key, self.lease)
            self.lease = None
        return won


def test_a_lease_written_just_after_the_withdrawal_mark_keeps_the_job() -> None:
    """The withdrawal reads the leases again after its mark, before it cancels anything.

    An attacher writes its lease before it reads the record: so either the
    withdrawal sees that lease and undoes its mark, or the attacher sees
    the mark (or the withdrawn record) and waits, then submits again.
    """
    with TemporaryDirectory() as directory:
        ledger = _LeasedOnceMarked(project=PROJECT)
        _, cluster, key, operation = _submitted(Path(directory), SELF, ledger)
        before = ledger.read(key)
        assert before is not None
        ledger.lease = LedgerLease(holder="run-a", renewed_at=NOW, expires_at=NOW + 300.0)

        stopping = Withdrawer(by=SELF, now=NOW, reason=f"its run {SELF} stopped wanting it")
        outcome = relinquish(ledger, cluster, key, stopping, since=NOW - GRACE_S)

        assert isinstance(outcome, Kept)
        entry = ledger.read(key)
        assert entry is not None
        assert entry.record == before.record
        assert entry.withdrawal is None
        assert [lease.holder for lease in entry.leases] == ["run-a"]
        observed = cluster.inspect(operation)
        assert isinstance(observed, ClusterObservation)
        assert not observed.status.terminal
