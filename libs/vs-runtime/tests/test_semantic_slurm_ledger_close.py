"""Closing a run's semantic Slurm executor lets go of its evaluation ledger requests.

The semantic executor makes one Slurm executor per candidate, all over the
run's one ledger attachment. Two evaluations of the same content share one
ledger request, and so one job; closing the run cancels that job only when no
other run still wants it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    SemanticEvaluationStage,
)
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, build_stack
from vs_sandbox.api.slurm import SlurmLedgerAttachment
from vs_sim.api.testing import ManualClock, SimThreads, VirtualClock, run_virtual
from vs_slurm.api import (
    ClusterObservation,
    FakeEvaluationLedger,
    LedgerLease,
    LedgerPolicy,
    LedgerState,
    SlurmConfig,
    SlurmJobStatus,
    SlurmSshTransport,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_slurm.api import LedgerEntry

PROJECT = "demo-0123456789ab"
HANDLES = ("eval-a", "eval-b")
# The stack's own cluster configuration, which the ledger key covers.
CONFIG = SlurmConfig(
    name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="test")
)


def _request(snapshot: str) -> EvaluationRequest:
    digest = ContentDigest.sha256(b"same")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    return EvaluationRequest(
        key="same-content",
        stages=tuple(
            EvaluationStep(
                name=kind.value,
                payload=SemanticEvaluationStage(
                    snapshot=snapshot, kind=kind, fingerprints=fingerprints
                ).model_dump(mode="json"),
            )
            for kind in (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK)
        ),
    )


@pytest.mark.parametrize("another_run_wants_it", [False, True])
def test_closing_the_run_cancels_the_shared_job_unless_another_run_wants_it(
    tmp_path: Path, *, another_run_wants_it: bool
) -> None:
    clock = VirtualClock(1000.0)
    ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())
    attachment = SlurmLedgerAttachment(
        ledger=ledger.reopen(),
        config=CONFIG,
        project=PROJECT,
        holder="run-1",
        policy=LedgerPolicy(),
        clock=clock,
    )
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.PENDING,)  # queued until cancelled

    async def main() -> LedgerEntry:
        stack = await build_stack(tmp_path, cluster, sleeper=clock, ledger=attachment)
        try:
            for handle in HANDLES:
                await stack.executor.submit(_request(stack.snapshot), handle_id=handle)
            await clock.sleep(30.0)  # both evaluations attach; the job is queued
            (key,) = ledger.list_keys()
            entry = ledger.read(key)
            assert entry is not None
            assert entry.record.state is LedgerState.SUBMITTED
            if another_run_wants_it:
                lease = LedgerLease(holder="run-2", renewed_at=clock.now(), expires_at=1e9)
                ledger.renew(key, lease)
        finally:
            await stack.executor.close()
        return entry

    entry = run_virtual(clock, main(), driver=SimThreads(clock, schedule_seed=None))

    assert cluster.submissions == [entry.record.operation_id]
    after = ledger.read(entry.record.key)
    assert after is not None
    assert "run-1" not in {lease.holder for lease in after.leases}
    observed = cluster.inspect(entry.record.operation_id)
    assert isinstance(observed, ClusterObservation)
    if another_run_wants_it:
        assert after.record.state is LedgerState.SUBMITTED
        assert not observed.status.terminal
    else:
        assert after.record.state is LedgerState.WITHDRAWN
        assert observed.status is SlurmJobStatus.CANCELLED
