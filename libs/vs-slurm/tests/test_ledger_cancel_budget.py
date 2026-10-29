"""A withdrawal's scancel lands while its mark still holds, however slow each transport call is.

The production cluster client runs every transport call against the Fake
connector, and each call costs up to one transport timeout of the withdrawer's
clock. Another run tries to attach on every call, as a run arriving after the
withdrawal started would, with a clock that runs ahead by the allowed skew: it
waits while the mark holds, undoes the mark once it expired, and attaches to
the job if the job is still live. The withdrawer must never cancel a job that
run attached to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    CLOCK_SKEW_SECONDS,
    AttachJob,
    AwaitWithdrawal,
    CheckJob,
    ClaimAttempt,
    ClusterCall,
    ClusterObservation,
    ClusterRejected,
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
    SlurmError,
    SlurmJobRunner,
    SlurmJobStatus,
    WithdrawalMark,
    WithdrawalTiming,
    WithdrawalUndone,
    Withdrawer,
    Withdrawn,
    WithdrawUnknown,
    evaluation_key,
    job_of_handle,
    ledger_operation_id,
    submit_step,
    undo_expired_withdrawal,
    withdraw,
)

if TYPE_CHECKING:
    from collections.abc import Callable

PROJECT = "demo-0123456789ab"
NOW = 1e6
_CALL_SECONDS = 110.0


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


@dataclass
class _LateAttacher:
    """A run that arrives once the withdrawal started, and tries to attach on every transport call."""

    ledger: FakeEvaluationLedger
    clock: ManualClock
    key: str
    started: bool = False
    attached: bool = False
    cancelled: bool = False
    violations: list[str] = field(default_factory=list)

    def now(self) -> float:
        """This run's clock: ahead of the withdrawer's by the whole allowed skew."""
        return self.clock.now() + CLOCK_SKEW_SECONDS

    def step(self) -> None:
        entry = self.ledger.read(self.key)
        if entry is None:
            return
        self.started = self.started or entry.withdrawal is not None
        if not self.started or self.attached or self.cancelled:
            return
        match submit_step(entry, now=self.now()):
            case AwaitWithdrawal(expired=True):
                undo_expired_withdrawal(self.ledger, entry, now=self.now())
                again = self.ledger.read(self.key)
                if again is not None and isinstance(
                    submit_step(again, now=self.now()), CheckJob | AttachJob
                ):
                    self.attached = True
            case CheckJob() | AttachJob():
                self.attached = True
            case _:
                pass

    def call(self, kind: ClusterCall, seconds: float) -> None:
        """One transport call of ``seconds``: this run tries to attach before its effect lands."""
        self.clock.advance(seconds)
        self.step()
        if kind is ClusterCall.SCANCEL:
            if self.attached:
                self.violations.append(f"scancel at {self.clock.now():.0f} killed a held job")
            self.cancelled = True

    def intercept(
        self, seconds: float
    ) -> Callable[[dict[str, object], Callable[[dict[str, object]], dict[str, object]]], dict]:
        """The connector's every request is one call of ``seconds``."""

        def slow(
            request: dict[str, object], answer: Callable[[dict[str, object]], dict[str, object]]
        ) -> dict[str, object]:
            scancel = request.get("operation") == "exec" and "scancel" in str(
                request.get("command")
            )
            self.call(ClusterCall.SCANCEL if scancel else ClusterCall.REMOTE_FILE, seconds)
            return answer(request)

        return slow


def _record(ledger: FakeEvaluationLedger, key: str, operation: str, handle: object) -> None:
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


def _world(
    root: Path, ledger: _SlowAfterMark, clock: ManualClock
) -> tuple[SlurmCluster, FakeConnector, str]:
    """A request whose job runs, submitted by the production client, recorded in ``ledger``."""
    remote = root / "remote"
    remote.mkdir()
    config = SlurmConfig(
        name="budget",
        remote_workspace_root=str(remote),
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        transport_timeout_seconds=int(_CALL_SECONDS),
    )
    connector = FakeConnector(root / "connector")
    runner = SlurmJobRunner(config, processes=connector, clock=clock)
    cluster = SlurmCluster(runner, state_root=root / "ids")
    workspace = root / "workspace"
    workspace.mkdir()
    request = SlurmBatchRequest(
        workspace=workspace, stages=(SlurmBatchStage(name="bench", command=("true",)),)
    )
    key = evaluation_key(PROJECT, request, config, setup_script_sha256=None)
    operation = ledger_operation_id(key, 0)
    connector.script(operation, states=(SlurmJobStatus.RUNNING,))
    submitted = cluster.submit(request, operation_id=operation)
    assert isinstance(submitted, ClusterSubmitted)
    _record(ledger, key, operation, submitted.handle)
    return cluster, connector, key


@settings(max_examples=examples(pr=15, nightly=150), deadline=None)
@given(
    call_share=st.floats(min_value=0.05, max_value=1.0),
    slowness=st.floats(min_value=0.0, max_value=900.0),
)
# The last check before the cancel passes with one call and the skew left; the
# cancel's squeue and scancel then take a whole timeout each.
@example(call_share=1.0, slowness=440.0)
def test_a_slow_multi_call_cancel_never_kills_a_job_a_run_attached_to_after_the_mark_expired(
    call_share: float, slowness: float
) -> None:
    """r3173-recheck MED-1: the cancel was budgeted one transport call but made two to five.

    Here the production client makes each of its calls, through the connector.
    """
    with TemporaryDirectory() as directory:
        clock = ManualClock(NOW)
        ledger = _SlowAfterMark(project=PROJECT, clock=ManualClock())
        ledger.clock = clock
        ledger.seconds = slowness
        cluster, connector, key = _world(Path(directory), ledger, clock)
        late = _LateAttacher(ledger.reopen(), clock, key)
        connector.intercept(late.intercept(call_share * _CALL_SECONDS))
        timing = WithdrawalTiming(clock=clock, call_seconds=_CALL_SECONDS)

        withdraw(ledger, cluster, key, Withdrawer(by="run-self", reason="stopped", timing=timing))

        assert late.violations == []


@settings(max_examples=examples(pr=30, nightly=300), deadline=None)
@given(
    call_share=st.floats(min_value=0.05, max_value=1.0),
    slowness=st.floats(min_value=0.0, max_value=900.0),
)
# One call and the skew left at the last check: a read before the scancel lands it too late.
@example(call_share=1.0, slowness=660.0)
def test_the_fake_cluster_cancel_spends_the_same_calls_and_never_kills_a_held_job(
    call_share: float, slowness: float
) -> None:
    """r3173-recheck MED-1, on the Fake cluster: its cancel makes the production client's calls."""
    with TemporaryDirectory() as directory:
        clock = ManualClock(NOW)
        ledger = _SlowAfterMark(project=PROJECT, clock=ManualClock())
        ledger.clock = clock
        ledger.seconds = slowness
        workspace = Path(directory)
        request = SlurmBatchRequest(
            workspace=workspace, stages=(SlurmBatchStage(name="bench", command=("true",)),)
        )
        config = SlurmConfig(
            name="budget",
            remote_workspace_root="/runs",
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        )
        key = evaluation_key(PROJECT, request, config, setup_script_sha256=None)
        late = _LateAttacher(ledger.reopen(), clock, key)
        cluster = FakeCluster(
            clock=clock, on_call=lambda kind: late.call(kind, call_share * _CALL_SECONDS)
        )
        operation = ledger_operation_id(key, 0)
        cluster.script(operation, states=(SlurmJobStatus.RUNNING,))
        submitted = cluster.submit(request, operation_id=operation)
        assert isinstance(submitted, ClusterSubmitted)
        _record(ledger, key, operation, submitted.handle)
        timing = WithdrawalTiming(clock=clock, call_seconds=_CALL_SECONDS)

        withdraw(ledger, cluster, key, Withdrawer(by="run-self", reason="stopped", timing=timing))

        assert late.violations == []


@settings(max_examples=examples(pr=20, nightly=200), deadline=None)
@given(slowness=st.floats(min_value=0.0, max_value=550.0))
def test_a_submitting_attempt_with_no_job_seen_is_withdrawn_first_however_slow_its_cancel(
    slowness: float,
) -> None:
    """With no job seen there is nothing to cancel by the mark's deadline.

    The attempt is withdrawn first, so no run attaches to it, and only then is
    its identity cancelled, however many slow calls that takes; a submitter
    that comes late finds the tombstone and starts nothing.
    """
    with TemporaryDirectory() as directory:
        clock = ManualClock(NOW)
        ledger = _SlowAfterMark(project=PROJECT, clock=ManualClock())
        ledger.clock = clock
        ledger.seconds = slowness
        request = SlurmBatchRequest(
            workspace=Path(directory), stages=(SlurmBatchStage(name="bench", command=("true",)),)
        )
        config = SlurmConfig(
            name="budget",
            remote_workspace_root="/runs",
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        )
        key = evaluation_key(PROJECT, request, config, setup_script_sha256=None)
        operation = ledger_operation_id(key, 0)
        late = _LateAttacher(ledger.reopen(), clock, key)
        cluster = FakeCluster(clock=clock, on_call=lambda kind: late.call(kind, _CALL_SECONDS))
        ledger.claim(
            LedgerRecord(
                key=key,
                project=PROJECT,
                attempt=0,
                operation_id=operation,
                owner="run-gone",
                created_at=NOW - 2000.0,
                claim_expires_at=NOW - 1100.0,
                state=LedgerState.SUBMITTING,
            )
        )
        timing = WithdrawalTiming(clock=clock, call_seconds=_CALL_SECONDS)

        outcome = withdraw(
            ledger, cluster, key, Withdrawer(by="run-self", reason="stopped", timing=timing)
        )

        assert isinstance(outcome, Withdrawn)
        entry = ledger.read(key)
        assert entry is not None
        assert entry.record.state is LedgerState.WITHDRAWN
        assert isinstance(cluster.submit(request, operation_id=operation), ClusterRejected)
        assert late.violations == []


@dataclass
class _Transport:
    """The Fake cluster's transport: every call fails while ``failing``."""

    failing: bool = False

    def __call__(self, _call: ClusterCall) -> None:
        if self.failing:
            raise SlurmError.transport_timed_out("exec")


@pytest.mark.parametrize("state", [SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING])
def test_a_failed_inspect_and_a_failed_cancel_never_leave_a_live_job_beside_the_next_attempts(
    state: SlurmJobStatus,
) -> None:
    """A dead submitter's job runs, unrecorded; the withdrawal's inspect and cancel both fail.

    The attempt must stay as it was, so the next ask resolves it rather than
    claiming a new attempt and starting a second live job beside the first.
    """
    with TemporaryDirectory() as directory:
        clock = ManualClock(NOW)
        transport = _Transport()
        cluster = FakeCluster(clock=clock, on_call=transport)
        ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())
        request = SlurmBatchRequest(
            workspace=Path(directory), stages=(SlurmBatchStage(name="bench", command=("true",)),)
        )
        config = SlurmConfig(
            name="budget",
            remote_workspace_root="/runs",
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        )
        key = evaluation_key(PROJECT, request, config, setup_script_sha256=None)
        first = ledger_operation_id(key, 0)
        cluster.script(first, states=(state,))
        # The submitter's sbatch went through; it died before it recorded the job.
        assert isinstance(cluster.submit(request, operation_id=first), ClusterSubmitted)
        ledger.claim(
            LedgerRecord(
                key=key,
                project=PROJECT,
                attempt=0,
                operation_id=first,
                owner="run-gone",
                created_at=NOW - 2000.0,
                claim_expires_at=NOW - 1100.0,
                state=LedgerState.SUBMITTING,
            )
        )
        timing = WithdrawalTiming(clock=clock, call_seconds=_CALL_SECONDS)

        transport.failing = True
        outcome = withdraw(
            ledger, cluster, key, Withdrawer(by="run-self", reason="stopped", timing=timing)
        )
        transport.failing = False

        entry = ledger.read(key)
        assert entry is not None
        operations = [first]
        step = submit_step(entry, now=clock.now())
        if isinstance(step, ClaimAttempt):  # the next ask starts the next attempt's job
            operations.append(ledger_operation_id(key, step.attempt))
            cluster.script(operations[-1], states=(SlurmJobStatus.RUNNING,))
            assert isinstance(
                cluster.submit(request, operation_id=operations[-1]), ClusterSubmitted
            )
        live = [
            operation
            for operation in operations
            if isinstance(observed := cluster.inspect(operation), ClusterObservation)
            and not observed.status.terminal
        ]
        assert len(live) <= 1, f"live jobs of attempts {live}"
        assert isinstance(outcome, WithdrawUnknown)
        assert entry.record.state is LedgerState.SUBMITTING
        assert entry.withdrawal is None
