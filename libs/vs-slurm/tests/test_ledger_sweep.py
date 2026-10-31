"""The sweep: against other processes' writes between its reads and writes, and its cost."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitted,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerEntry,
    LedgerLease,
    LedgerRecord,
    LedgerState,
    OrphanChecks,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmJobStatus,
    SlurmSshTransport,
    WithdrawalTiming,
    Withdrawn,
    evaluation_key,
    job_of_handle,
    ledger_operation_id,
    sweep_unleased,
    sweep_withdrawer,
    transitioned,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_slurm.api import (
        ClusterInspectOutcome,
        ClusterTarget,
        WithdrawalMark,
        WithdrawalUndone,
    )

PROJECT = "demo-0123456789ab"
CONFIG = SlurmConfig(
    name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="h")
)
NOW = 1e6


# One transport call to the cluster may take this long (``transport_timeout_seconds``).
_CALL_SECONDS = 110.0


def _timing(now: float) -> WithdrawalTiming:
    """A withdrawer's timing, its clock standing at ``now``."""
    return WithdrawalTiming(clock=ManualClock(now), call_seconds=_CALL_SECONDS)


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

    def write_withdrawal(
        self, current: LedgerEntry, document: WithdrawalMark | WithdrawalUndone
    ) -> bool:
        return self._inner.write_withdrawal(current, document)

    def renew(self, key: str, lease: LedgerLease) -> None:
        self._inner.renew(key, lease)

    def release(self, key: str, holder: str) -> None:
        self._inner.release(key, holder)

    def list_keys(self) -> tuple[str, ...]:
        return self._inner.list_keys()

    def operator_file_sha256(self, path: str) -> str | None:
        return self._inner.operator_file_sha256(path)

    def bind_handle(self, holder: str, handle: str, key: str) -> str:
        return self._inner.bind_handle(holder, handle, key)

    def bound_key(self, holder: str, handle: str) -> str | None:
        return self._inner.bound_key(holder, handle)


class _Unleased:
    """One request whose job runs and whose only lease expired long before ``NOW``."""

    def __init__(self, root: Path) -> None:
        self.cluster = FakeCluster(clock=ManualClock())
        self.ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())
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
        outcomes = sweep_unleased(
            racing,
            world.cluster,
            sweeper=sweep_withdrawer(_timing(NOW), grace_seconds=0.0),
            grace_seconds=0.0,
            orphans=OrphanChecks(900.0),
        )

        assert outcomes == ()
        if kind == "attach":
            assert world.state() is LedgerState.SUBMITTED
            assert world.job_status() is SlurmJobStatus.RUNNING
        else:
            assert world.state() is LedgerState.COMPLETED
        if after_read == 1:
            assert world.job_status() is SlurmJobStatus.RUNNING


class _CountingCluster(FakeCluster):
    """The Fake cluster, counting the scheduler queries (one SSH round trip each) made of it."""

    def __init__(self) -> None:
        super().__init__(clock=ManualClock())
        self.inspections = 0

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        self.inspections += 1
        return super().inspect(target, by_job_id=by_job_id)


WINDOW = 900.0


def _jobless_failures(root: Path, attempts: int) -> tuple[FakeEvaluationLedger, SlurmBatchRequest]:
    """A request whose ``attempts`` attempts all failed before naming a job."""
    ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    request = SlurmBatchRequest(
        workspace=workspace, stages=(SlurmBatchStage(name="bench", command=("true",)),)
    )
    key = evaluation_key(PROJECT, request, CONFIG, setup_script_sha256=None)
    for attempt in range(attempts):
        claimed = LedgerRecord(
            key=key,
            project=PROJECT,
            attempt=attempt,
            operation_id=ledger_operation_id(key, attempt),
            owner="run-a",
            created_at=0.0,
            claim_expires_at=WINDOW,
            state=LedgerState.SUBMITTING,
        )
        ledger.claim(claimed)
        entry = ledger.read(key)
        assert entry is not None
        assert ledger.replace(entry, transitioned(claimed, LedgerState.FAILED, reason="rejected"))
    return ledger, request


@given(
    attempts=st.integers(min_value=1, max_value=4),
    gaps=st.lists(st.floats(min_value=0.0, max_value=WINDOW), min_size=1, max_size=8),
)
@example(attempts=2, gaps=[0.0, WINDOW, 1.0, 1.0])
@settings(max_examples=examples(pr=30, nightly=300), deadline=None)
def test_a_process_inspects_a_jobless_attempt_for_one_claim_window_then_never(
    attempts: int, gaps: list[float]
) -> None:
    """Each sweep runs in one evaluation's heartbeat, so its cost must not grow with history.

    An attempt that failed before naming a job is inspected for an orphan only
    while this process has known it ended for at most the claim window; after
    that the sweep makes no cluster call for it at all.
    """
    with TemporaryDirectory() as directory:
        ledger, _ = _jobless_failures(Path(directory), attempts)
        cluster = _CountingCluster()
        orphans = OrphanChecks(window_seconds=WINDOW)
        first = NOW + gaps[0]
        now = NOW
        for gap in gaps:
            now += gap
            before = cluster.inspections
            assert (
                sweep_unleased(
                    ledger,
                    cluster,
                    sweeper=sweep_withdrawer(_timing(now), grace_seconds=0.0),
                    grace_seconds=0.0,
                    orphans=orphans,
                )
                == ()
            )
            expected = attempts if now - first <= WINDOW else 0
            assert cluster.inspections - before == expected


@pytest.mark.parametrize("appears_after", [0.0, WINDOW / 2, WINDOW])
def test_an_orphan_that_starts_within_the_window_is_still_cancelled(appears_after: float) -> None:
    """A submitter past the tombstone sbatches after the sweep first looked, then dies."""
    with TemporaryDirectory() as directory:
        ledger, request = _jobless_failures(Path(directory), 1)
        (key,) = ledger.list_keys()
        operation = ledger_operation_id(key, 0)
        cluster = _CountingCluster()
        orphans = OrphanChecks(window_seconds=WINDOW)
        assert (
            sweep_unleased(
                ledger,
                cluster,
                sweeper=sweep_withdrawer(_timing(NOW), grace_seconds=0.0),
                grace_seconds=0.0,
                orphans=orphans,
            )
            == ()
        )
        cluster.script(operation, states=(SlurmJobStatus.RUNNING,))
        assert isinstance(cluster.submit(request, operation_id=operation), ClusterSubmitted)

        later = NOW + appears_after
        outcomes = sweep_unleased(
            ledger,
            cluster,
            sweeper=sweep_withdrawer(_timing(later), grace_seconds=0.0),
            grace_seconds=0.0,
            orphans=orphans,
        )

        (outcome,) = outcomes
        assert isinstance(outcome, Withdrawn)
        assert outcome.operation_id == operation
        observed = cluster.inspect(operation)
        assert isinstance(observed, ClusterObservation)
        assert observed.status.terminal
        # Once a sweep saw its job ended (or the window closed), it is never inspected again.
        sweep_unleased(
            ledger,
            cluster,
            sweeper=sweep_withdrawer(_timing(later + 1.0), grace_seconds=0.0),
            grace_seconds=0.0,
            orphans=orphans,
        )
        before = cluster.inspections
        sweep_unleased(
            ledger,
            cluster,
            sweeper=sweep_withdrawer(_timing(later + 2.0), grace_seconds=0.0),
            grace_seconds=0.0,
            orphans=orphans,
        )
        assert cluster.inspections == before
