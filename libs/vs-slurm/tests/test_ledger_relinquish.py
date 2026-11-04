"""A run that stops wanting a request cancels its job exactly when no other run still wants it."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    CLOCK_SKEW_SECONDS,
    ClusterObservation,
    ClusterSubmitted,
    ClusterUnknown,
    FakeCluster,
    FakeEvaluationLedger,
    Kept,
    LedgerEntry,
    LedgerError,
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
    WithdrawalTiming,
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


# One transport call to the cluster may take this long (``transport_timeout_seconds``).
_CALL_SECONDS = 110.0


def _timing(now: float) -> WithdrawalTiming:
    """A withdrawer's timing, its clock standing at ``now``."""
    return WithdrawalTiming(clock=ManualClock(now), call_seconds=_CALL_SECONDS)


def _submitted(
    root: Path, owner: str, ledger: FakeEvaluationLedger | None = None
) -> tuple[FakeEvaluationLedger, FakeCluster, str, str]:
    """A request whose job is queued, claimed by ``owner`` and leased by this run."""
    cluster = FakeCluster(clock=ManualClock(NOW))
    ledger = (
        ledger if ledger is not None else FakeEvaluationLedger(project=PROJECT, clock=ManualClock())
    )
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

        stopping = Withdrawer(
            by=SELF, reason=f"its run {SELF} stopped wanting it", timing=_timing(NOW)
        )
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

    def cancel(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCancelOutcome:
        del observed  # lost on the way, whatever reading the caller holds
        named = target if isinstance(target, str) and not by_job_id else None
        return ClusterUnknown(operation_id=named, reason="ssh: connection reset")


def _withdrawer(by: str, *, now: float = NOW) -> Withdrawer:
    return Withdrawer(by=by, reason=f"{by} withdrew it", timing=_timing(now))


def _mark(ledger: FakeEvaluationLedger, key: str, by: str, *, since: float) -> WithdrawalMark:
    """Mark the request's attempt by ``by`` from ``since``, as a withdrawer that then died."""
    entry = ledger.read(key)
    assert entry is not None
    mark = WithdrawalMark(
        attempt=entry.record.attempt,
        by=by,
        writer="0f3c9e1",
        since=since,
        until=since + _timing(since).window_seconds,
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

        late = NOW + _timing(NOW).window_seconds + 1.0
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
        ledger = _LeasedOnceMarked(project=PROJECT, clock=ManualClock())
        _, cluster, key, operation = _submitted(Path(directory), SELF, ledger)
        before = ledger.read(key)
        assert before is not None
        ledger.lease = LedgerLease(holder="run-a", renewed_at=NOW, expires_at=NOW + 300.0)

        stopping = Withdrawer(
            by=SELF, reason=f"its run {SELF} stopped wanting it", timing=_timing(NOW)
        )
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


def test_a_withdrawal_window_covers_its_calls_at_the_transport_timeout_and_the_clock_skew() -> None:
    """Derived from the transport timeout: six calls from the stamp to the cancel, one spare.

    A timeout whose window outlasts ``MAX_WITHDRAWAL_WINDOW_SECONDS`` is refused
    (``test_a_withdrawal_timing_whose_mark_outlasts_the_longest_window_is_refused``).
    """
    for call_seconds in (1.0, 30.0, 110.0, 120.0):
        timing = WithdrawalTiming(clock=ManualClock(NOW), call_seconds=call_seconds)
        assert timing.window_seconds == 7 * call_seconds + CLOCK_SKEW_SECONDS


class _SlowAfterMark(FakeEvaluationLedger):
    """The Fake ledger, where every read after a mark takes ``seconds`` of the withdrawer's clock."""

    clock: ManualClock
    seconds: float = 0.0
    marked: bool = False

    def read(self, key: str) -> LedgerEntry | None:
        if self.marked:
            self.clock.advance(self.seconds)
        return super().read(key)

    def write_withdrawal(
        self, current: LedgerEntry, document: WithdrawalMark | WithdrawalUndone
    ) -> bool:
        won = super().write_withdrawal(current, document)
        self.marked = self.marked or (won and isinstance(document, WithdrawalMark))
        return won


@settings(max_examples=examples(pr=20, nightly=200), deadline=None)
@given(
    call_seconds=st.sampled_from((30.0, 110.0)),
    slowness=st.floats(min_value=0.0, max_value=1.5),
)
# Just too slow: one call short of the margin the inspect and the cancel need.
@example(call_seconds=110.0, slowness=(7 * 110.0 - 2 * 110.0) / (7 * 110.0 + 60.0) + 1e-6)
def test_a_withdrawer_left_too_little_of_its_window_cancels_nothing_and_undoes_its_mark(
    call_seconds: float, slowness: float
) -> None:
    """r3173 MED-2: a mark stamped early, or read back late, must never cancel past its window.

    Past the window another process undoes the mark and a run may attach to the
    job. So the job is cancelled only while the mark holds, on every clock, for
    the inspect and the cancel still to come; a slower withdrawal undoes its
    mark and leaves the job running.
    """
    with TemporaryDirectory() as directory:
        clock = ManualClock(NOW)
        ledger = _SlowAfterMark(project=PROJECT, clock=ManualClock())
        ledger.clock = clock
        _, cluster, key, operation = _submitted(Path(directory), SELF, ledger)
        timing = WithdrawalTiming(clock=clock, call_seconds=call_seconds)
        ledger.seconds = slowness * timing.window_seconds
        remaining = timing.window_seconds - ledger.seconds
        in_time = remaining >= 2 * call_seconds + CLOCK_SKEW_SECONDS

        outcome = withdraw(
            ledger, cluster, key, Withdrawer(by=SELF, reason="stopped", timing=timing)
        )

        observed = cluster.inspect(operation)
        assert isinstance(observed, ClusterObservation)
        entry = ledger.read(key)
        assert entry is not None
        if in_time:
            assert isinstance(outcome, Withdrawn)
            assert observed.status is SlurmJobStatus.CANCELLED
        else:
            assert isinstance(outcome, WithdrawUnknown)
            assert not observed.status.terminal
            assert entry.record.state is LedgerState.SUBMITTED
            assert entry.withdrawal is None  # undone: a run may attach at once


class _MarkAnswerLost(FakeEvaluationLedger):
    """The Fake ledger, where the answer to the first mark write is lost after it landed."""

    lost: bool = False

    def write_withdrawal(
        self, current: LedgerEntry, document: WithdrawalMark | WithdrawalUndone
    ) -> bool:
        won = super().write_withdrawal(current, document)
        if isinstance(document, WithdrawalMark) and not self.lost:
            self.lost = True
            raise LedgerError.transport(ConnectionResetError("ssh: connection reset"))
        return won


def test_a_withdrawer_whose_mark_answer_was_lost_knows_the_mark_for_its_own() -> None:
    """r3173 LOW-5: a lost answer left the withdrawer waiting on its own mark until it expired.

    The mark names its writer, so the withdrawer's next read finds its own
    mark and goes on. Another withdrawer of the same name, a sibling process,
    still waits on it.
    """
    with TemporaryDirectory() as directory:
        ledger = _MarkAnswerLost(project=PROJECT, clock=ManualClock())
        _, cluster, key, operation = _submitted(Path(directory), SELF, ledger)
        stopping = _withdrawer(SELF)

        outcome = withdraw(ledger, cluster, key, stopping)

        assert isinstance(outcome, Withdrawn)
        observed = cluster.inspect(operation)
        assert isinstance(observed, ClusterObservation)
        assert observed.status is SlurmJobStatus.CANCELLED


def test_a_mark_left_by_a_withdrawers_earlier_call_is_its_own_and_no_one_elses() -> None:
    """A relinquish tried again with the same withdrawer finishes the mark its earlier try left."""
    with TemporaryDirectory() as directory:
        ledger, cluster, key, operation = _submitted(Path(directory), SELF)
        earlier = _withdrawer(SELF)
        entry = ledger.read(key)
        assert entry is not None
        assert ledger.write_withdrawal(entry, earlier.stamped(entry.record.attempt))

        namesake = withdraw(ledger, cluster, key, _withdrawer(SELF))
        assert isinstance(namesake, WithdrawUnknown)
        again = withdraw(ledger, cluster, key, earlier)
        assert isinstance(again, Withdrawn)
        observed = cluster.inspect(operation)
        assert isinstance(observed, ClusterObservation)
        assert observed.status is SlurmJobStatus.CANCELLED
