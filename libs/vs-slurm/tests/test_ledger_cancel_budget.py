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
    ClusterSubmitted,
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

    def intercept(
        self, seconds: float
    ) -> Callable[[dict[str, object], Callable[[dict[str, object]], dict[str, object]]], dict]:
        """Each transport call takes ``seconds``; its effect lands when it returns."""

        def slow(
            request: dict[str, object], answer: Callable[[dict[str, object]], dict[str, object]]
        ) -> dict[str, object]:
            self.clock.advance(seconds)
            self.step()
            if request.get("operation") == "exec" and "scancel" in str(request.get("command")):
                if self.attached:
                    self.violations.append(f"scancel at {self.clock.now():.0f} killed a held job")
                self.cancelled = True
            return answer(request)

        return slow


def _world(root: Path, ledger: _SlowAfterMark, clock: ManualClock) -> tuple[SlurmCluster, FakeConnector, str]:
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
    assert isinstance(submitted.handle, SlurmBatchHandle)
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
            job=job_of_handle(submitted.handle),
        )
    )
    return cluster, connector, key


@pytest.mark.xfail(strict=True, reason="r3173-recheck MED-1: the cancel's calls are not budgeted")
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
    """r3173-recheck MED-1: the cancel was budgeted one transport call but makes two to five."""
    with TemporaryDirectory() as directory:
        clock = ManualClock(NOW)
        ledger = _SlowAfterMark(project=PROJECT)
        ledger.clock = clock
        ledger.seconds = slowness
        cluster, connector, key = _world(Path(directory), ledger, clock)
        late = _LateAttacher(ledger.reopen(), clock, key)
        connector.intercept(late.intercept(call_share * _CALL_SECONDS))
        timing = WithdrawalTiming(clock=clock, call_seconds=_CALL_SECONDS)

        withdraw(ledger, cluster, key, Withdrawer(by="run-self", reason="stopped", timing=timing))

        assert late.violations == []
