"""A run that stops wanting a request cancels its job exactly when no other run still wants it."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitted,
    FakeCluster,
    FakeEvaluationLedger,
    Kept,
    LedgerLease,
    LedgerRecord,
    LedgerState,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmJobStatus,
    SlurmSshTransport,
    Withdrawn,
    evaluation_key,
    job_of_handle,
    ledger_operation_id,
    relinquish,
)

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


def _submitted(root: Path, owner: str) -> tuple[FakeEvaluationLedger, FakeCluster, str, str]:
    """A request whose job is queued, claimed by ``owner`` and leased by this run."""
    cluster = FakeCluster(clock=ManualClock(NOW))
    ledger = FakeEvaluationLedger(project=PROJECT)
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

        outcome = relinquish(ledger, cluster, key, holder=SELF, since=NOW - GRACE_S)

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
