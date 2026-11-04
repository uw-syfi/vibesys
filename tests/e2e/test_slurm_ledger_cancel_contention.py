"""A withdrawal's scancel is never held up behind another thread of the same run.

r3206 MED-1: ``SlurmCluster.cancel`` took the run's operation store lock (a
blocking ``flock`` on ``<state_root>/operations.lock``) before it sent the
scancel. The withdrawal's last time check ran before that wait, so the wait was
not budgeted. Another thread of the run holds the lock across slow scheduler
reads (each evaluation's poll inspects its job under it), so the scancel could
land after the withdrawal's mark expired, and kill a job that another run had
attached to meanwhile. ``FakeCluster`` had the same shape: its scancel waited
for the lock every inspection holds across its transport calls.

This is a real-tier test: it needs a second real thread and the real file lock.
The thread polls another evaluation of the run and blocks in its first
transport call, holding whatever the implementation holds there, until the
withdrawal's scancel has gone out, or until the withdrawal is seen waiting for
that thread (on the production client, ``/proc/locks`` shows a waiter on the
lock file). Then that read takes ``_OTHER_READ_S`` of the withdrawer's clock and
ends. A late run, whose clock runs ahead by the allowed skew, tries to attach
on each of the withdrawer's transport calls; the withdrawer must never send a
scancel once that run attached.
"""

from __future__ import annotations

import os
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_sim.api.testing import HANG_GUARD_S, ManualClock
from vs_slurm.api import (
    CLOCK_SKEW_SECONDS,
    AttachJob,
    AwaitWithdrawal,
    CheckJob,
    ClusterCall,
    ClusterSubmitted,
    FakeCluster,
    FakeConnector,
    FakeEvaluationLedger,
    LedgerEntry,
    LedgerRecord,
    LedgerState,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmCluster,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobRunner,
    SlurmJobStatus,
    WithdrawalMark,
    WithdrawalTiming,
    WithdrawalUndone,
    Withdrawer,
    evaluation_key,
    job_of_handle,
    ledger_operation_id,
    submit_step,
    undo_expired_withdrawal,
    withdraw,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_slurm.api import ClusterCancelOutcome, ClusterObservation, ClusterTarget

PROJECT = "demo-0123456789ab"
NOW = 1e6
_CALL_S = 110.0
# The withdrawer's clock: each of its transport calls, and each ledger read after its mark.
# With these, its last check before the cancel passes with 180 s of the 830 s mark left.
_WITHDRAWER_CALL_S = 50.0
_READ_AFTER_MARK_S = 550.0
# The rest of the other thread's read once the withdrawal reaches its cancel: a squeue
# that times out, and its retry.
_OTHER_READ_S = 150.0


class _SlowAfterMark(FakeEvaluationLedger):
    """The withdrawer's ledger, where every read after its mark takes ``seconds`` of its clock."""

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


@dataclass
class _World:
    """The two threads of one run, and a late run that tries to attach to the withdrawn request."""

    clock: ManualClock
    late_ledger: FakeEvaluationLedger
    key: str
    lock_file: Path | None
    withdrawer: int = field(default_factory=threading.get_ident)
    other_holds: threading.Event = field(default_factory=threading.Event)
    scancel_sent: threading.Event = field(default_factory=threading.Event)
    attached: bool = False
    cancelled: bool = False
    violations: list[str] = field(default_factory=list)

    def withdrawer_call(self, kind: ClusterCall) -> None:
        """One of the withdrawer's transport calls: the late run tries to attach before it lands."""
        self.clock.advance(_WITHDRAWER_CALL_S)
        self._late_run_step()
        if kind is ClusterCall.SCANCEL:
            if self.attached and not self.cancelled:
                self.violations.append(f"scancel at {self.clock.now():.0f} killed a held job")
            self.cancelled = True

    def other_call(self) -> None:
        """The other thread's first call: it holds on until the withdrawal sent or waits, then ends."""
        if self.other_holds.is_set():
            return
        self.other_holds.set()
        deadline = time.monotonic() + HANG_GUARD_S
        # The loop ends on an event: the scancel sent, or the withdrawer queued on the
        # flock in /proc/locks. A correct cancel sends its scancel first, so expiry never
        # decides a pass.
        # test-isolation: HANG_GUARD_S only bounds a hang; the verdict rests on events.
        while time.monotonic() < deadline:
            if self.scancel_sent.wait(0.01) or self._withdrawer_waits_for_the_lock():
                break
        self.clock.advance(_OTHER_READ_S)

    def on_call(self, kind: ClusterCall) -> None:
        """A transport call of the Fake cluster; its effect lands once this returns."""
        if threading.get_ident() != self.withdrawer:
            self.other_call()
            return
        self.withdrawer_call(kind)
        if kind is ClusterCall.SCANCEL:
            self.scancel_sent.set()

    def intercept(
        self, request: dict[str, object], answer: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        """A request of the production client to the connector."""
        program = shlex.split(str(request.get("command") or ""))[:1]
        scancel = request.get("operation") == "exec" and program == ["scancel"]
        if threading.get_ident() != self.withdrawer:
            self.other_call()
            return answer(request)
        self.withdrawer_call(ClusterCall.SCANCEL if scancel else ClusterCall.REMOTE_FILE)
        response = answer(request)
        if scancel:
            self.scancel_sent.set()  # its effect landed
        return response

    def _withdrawer_waits_for_the_lock(self) -> bool:
        """Whether some thread waits for the operation store's ``flock`` (``/proc/locks``)."""
        if self.lock_file is None or not self.lock_file.exists():
            return False
        status = self.lock_file.stat()
        # /proc/locks names a file as MAJOR:MINOR:INODE, the device numbers in hex:
        # match the device too, since another filesystem may hold the same inode.
        lock = (os.major(status.st_dev), os.minor(status.st_dev), status.st_ino)
        for line in Path("/proc/locks").read_text(encoding="utf-8").splitlines():
            tokens = line.split()
            if "->" not in tokens:
                continue
            major, minor, inode = tokens[tokens.index("->") + 5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == lock:
                return True
        return False

    def _late_run_step(self) -> None:
        if self.attached or self.cancelled:
            return
        entry = self.late_ledger.read(self.key)
        if entry is None or (entry.withdrawal is None and entry.withdrawal_revision < 0):
            return  # the withdrawal has not started yet
        now = self.clock.now() + CLOCK_SKEW_SECONDS
        match submit_step(entry, now=now):
            case AwaitWithdrawal(expired=True):
                undo_expired_withdrawal(self.late_ledger, entry, now=now)
                again = self.late_ledger.read(self.key)
                if again is not None and isinstance(
                    submit_step(again, now=now), CheckJob | AttachJob
                ):
                    self.attached = True
            case CheckJob() | AttachJob():
                self.attached = True
            case _:
                pass


def _request(workspace: Path, name: str) -> SlurmBatchRequest:
    return SlurmBatchRequest(
        workspace=workspace, stages=(SlurmBatchStage(name=name, command=("true",)),)
    )


def _config(remote: Path) -> SlurmConfig:
    return SlurmConfig(
        name="contention",
        remote_workspace_root=str(remote),
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        transport_timeout_seconds=int(_CALL_S),
    )


class _ContendedSlurmCluster(SlurmCluster):
    """The production client, where another thread of the run polls ``other`` as a cancel starts."""

    other: str
    world: _World

    def cancel(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCancelOutcome:
        return _contended(
            self,
            self.world,
            self.other,
            lambda: super(_ContendedSlurmCluster, self).cancel(
                target, by_job_id=by_job_id, observed=observed
            ),
        )


class _ContendedFakeCluster(FakeCluster):
    """The Fake, where another thread of the run polls ``other`` as a cancel starts."""

    other: str
    world: _World

    def cancel(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCancelOutcome:
        return _contended(
            self,
            self.world,
            self.other,
            lambda: super(_ContendedFakeCluster, self).cancel(
                target, by_job_id=by_job_id, observed=observed
            ),
        )


def _contended(
    cluster: SlurmCluster | FakeCluster,
    world: _World,
    other: str,
    cancel: Callable[[], ClusterCancelOutcome],
) -> ClusterCancelOutcome:
    """Run ``cancel`` once the other thread is inside its read of ``other``."""
    poller = threading.Thread(target=cluster.inspect, args=(other,), name="other-evaluation-poll")
    poller.start()
    try:
        assert world.other_holds.wait(HANG_GUARD_S), "the other thread never started its read"
        return cancel()
    finally:
        poller.join(HANG_GUARD_S)
        assert not poller.is_alive(), "the other thread's read never ended"


def _claim(ledger: FakeEvaluationLedger, key: str, operation: str, handle: object) -> None:
    assert isinstance(handle, SlurmBatchHandle)
    ledger.claim(
        LedgerRecord(
            key=key,
            project=PROJECT,
            attempt=0,
            operation_id=operation,
            owner="run-self",
            created_at=NOW - 10.0,
            claim_expires_at=NOW + 890.0,
            state=LedgerState.SUBMITTED,
            job=job_of_handle(handle),
        )
    )


def _cluster(
    implementation: str, root: Path, world: _World, operations: tuple[str, ...]
) -> _ContendedSlurmCluster | _ContendedFakeCluster:
    """The implementation, with each of ``operations`` scripted to run until cancelled."""
    if implementation == "fake":
        fake = _ContendedFakeCluster(clock=world.clock, on_call=world.on_call)
        for operation in operations:
            fake.script(operation, states=(SlurmJobStatus.RUNNING,))
        return fake
    connector = FakeConnector(root / "connector")
    for operation in operations:
        connector.script(operation, states=(SlurmJobStatus.RUNNING,))
    connector.intercept(world.intercept)
    runner = SlurmJobRunner(_config(root / "remote"), processes=connector, clock=world.clock)
    return _ContendedSlurmCluster(runner, state_root=root / "ids")


@pytest.mark.parametrize("implementation", ["slurm", "fake"])
def test_a_withdrawals_scancel_is_never_held_up_behind_another_thread_of_the_run(
    tmp_path: Path, implementation: str
) -> None:
    clock = ManualClock(NOW)
    ledger = _SlowAfterMark(project=PROJECT, clock=ManualClock())
    ledger.clock = clock
    ledger.seconds = _READ_AFTER_MARK_S
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "remote").mkdir()
    config = _config(tmp_path / "remote")
    mine, theirs = _request(workspace, "mine"), _request(workspace, "theirs")
    key = evaluation_key(PROJECT, mine, config, setup_script_sha256=None)
    operation = ledger_operation_id(key, 0)
    other = ledger_operation_id(
        evaluation_key(PROJECT, theirs, config, setup_script_sha256=None), 0
    )
    lock_file = tmp_path / "ids" / "operations.lock" if implementation == "slurm" else None
    world = _World(clock, ledger.reopen(), key, lock_file)
    cluster = _cluster(implementation, tmp_path, world, (operation, other))
    # Submitted before the withdrawal starts, so the late run does not try to attach yet.
    submitted = cluster.submit(mine, operation_id=operation)
    assert isinstance(submitted, ClusterSubmitted)
    assert isinstance(cluster.submit(theirs, operation_id=other), ClusterSubmitted)
    _claim(ledger, key, operation, submitted.handle)
    cluster.other, cluster.world = other, world
    timing = WithdrawalTiming(clock=clock, call_seconds=_CALL_S)
    start = clock.now()

    withdraw(ledger, cluster, key, Withdrawer(by="run-self", reason="stopped", timing=timing))

    assert world.other_holds.is_set()
    assert world.cancelled, "the withdrawal never sent its scancel"
    assert world.violations == [], f"withdrawal started at {start:.0f}"
