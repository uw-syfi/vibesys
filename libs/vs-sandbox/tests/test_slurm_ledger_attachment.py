"""A run's ledger attachment across the crash windows of a submission (#2757).

A submission is three steps: claim the request's attempt, sbatch it under a
name derived from the key, record the job. A process that dies after any step
leaves a state the next asker must finish, never duplicate.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sandbox.api.slurm import Attached, SlurmLedgerAttachment
from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitted,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerLabels,
    LedgerPolicy,
    LedgerRecord,
    LedgerState,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmFileArtifact,
    SlurmSshTransport,
    SlurmSubmissionRejectedError,
    SlurmTreeArtifact,
    ledger_operation_id,
)

PROJECT = "demo-0123456789ab"
CONFIG = SlurmConfig(
    name="fake-cluster",
    remote_workspace_root="/runs",
    transport=SlurmSshTransport(host="fake-cluster"),
)


class _World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.clock = ManualClock(1000.0)
        self.cluster = FakeCluster(clock=self.clock)
        self.ledger = FakeEvaluationLedger(project=PROJECT)

    def attachment(self, holder: str) -> SlurmLedgerAttachment:
        return SlurmLedgerAttachment(
            ledger=self.ledger.reopen(),
            config=CONFIG,
            project=PROJECT,
            holder=holder,
            policy=LedgerPolicy(),
            clock=self.clock,
        )

    def request(self, run: str, artifacts: tuple[str, ...] = ()) -> SlurmBatchRequest:
        """The benchmark request, from this run's own worktree, declaring ``artifacts``.

        A name ending in ``/`` is a tree artifact, any other a file artifact.
        """
        workspace = self.root / run / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "bench.py").write_text("print('bench')", encoding="utf-8")
        return SlurmBatchRequest(
            workspace=workspace,
            stages=(
                SlurmBatchStage(
                    name="benchmark",
                    command=("python", "bench.py"),
                    file_artifacts=tuple(
                        SlurmFileArtifact(
                            remote_path=name, local_path=workspace / name, collect_on_failure=True
                        )
                        for name in artifacts
                        if not name.endswith("/")
                    ),
                    tree_artifacts=tuple(
                        SlurmTreeArtifact(
                            remote_path=name.rstrip("/"), local_path=workspace / name.rstrip("/")
                        )
                        for name in artifacts
                        if name.endswith("/")
                    ),
                ),
            ),
        )

    def claim(self, holder: str, key: str, attempt: int = 0) -> LedgerRecord:
        record = LedgerRecord(
            key=key,
            project=PROJECT,
            attempt=attempt,
            operation_id=ledger_operation_id(key, attempt),
            owner=holder,
            created_at=self.clock.now(),
            labels=LedgerLabels(),
            state=LedgerState.SUBMITTING,
        )
        assert self.ledger.claim(record) == record
        return record

    def state(self, key: str) -> LedgerState:
        entry = self.ledger.read(key)
        assert entry is not None
        return entry.record.state


_ARTIFACTS = st.lists(
    st.sampled_from(("result.json", "metrics.txt", "traces/", "profile/")), unique=True, max_size=3
).map(tuple)


@given(crash_after=st.sampled_from(("nothing", "claim", "sbatch", "record")), artifacts=_ARTIFACTS)
@settings(max_examples=examples(pr=12, nightly=60), deadline=None)
def test_whichever_step_a_submitter_died_after_the_next_asker_gets_one_job(
    crash_after: str, artifacts: tuple[str, ...]
) -> None:
    """The next asker runs from another worktree; the job's artifacts land in its own."""
    with TemporaryDirectory() as directory:
        world = _World(Path(directory))
        dead = world.attachment("run-dead")
        key = dead.key(world.request("run-dead", artifacts))
        operation = ledger_operation_id(key, 0)
        if crash_after == "record":
            assert isinstance(
                dead.attach(
                    world.cluster, world.request("run-dead", artifacts), labels=LedgerLabels()
                ),
                Attached,
            )
        elif crash_after in {"claim", "sbatch"}:
            world.claim("run-dead", key)
            if crash_after == "sbatch":
                submitted = world.cluster.submit(
                    world.request("run-dead", artifacts), operation_id=operation
                )
                assert isinstance(submitted, ClusterSubmitted)

        attached = world.attachment("run-next").attach(
            world.cluster, world.request("run-next", artifacts), labels=LedgerLabels()
        )

        assert isinstance(attached, Attached)
        assert attached.operation_id == operation
        assert attached.handle.stages[0].name == "benchmark"
        _assert_lands_in(attached, world.request("run-next", artifacts))
        assert world.state(key) is LedgerState.SUBMITTED
        observed = world.cluster.inspect(operation)
        assert isinstance(observed, ClusterObservation)
        assert observed.job_id == attached.handle.job.job_id
        # The asker's job is the dead submitter's job, if it had one: one job in all.
        again = world.attachment("run-third").attach(
            world.cluster, world.request("run-third", artifacts), labels=LedgerLabels()
        )
        assert isinstance(again, Attached)
        assert again.handle.job.job_id == attached.handle.job.job_id
        _assert_lands_in(again, world.request("run-third", artifacts))


def _assert_lands_in(attached: Attached, request: SlurmBatchRequest) -> None:
    """Every artifact lands where this asker's request declares, inside its own workspace."""
    declared = {
        (item.remote_path, item.local_path)
        for stage in request.stages
        for item in (*stage.file_artifacts, *stage.tree_artifacts)
    }
    landed = {
        (item.remote_path, item.local_path)
        for stage in attached.handle.stages
        for item in (*stage.file_artifacts, *stage.tree_artifacts)
    }
    assert landed == declared
    assert all(local.is_relative_to(request.workspace) for _, local in landed)


def test_a_rejected_submission_withdraws_its_attempt_and_the_next_ask_claims_a_new_one(
    tmp_path: Path,
) -> None:
    world = _World(tmp_path)
    run = world.attachment("run-a")
    key = run.key(world.request("run-a"))
    world.cluster.script(ledger_operation_id(key, 0), rejected_reason="invalid partition")

    with pytest.raises(SlurmSubmissionRejectedError):
        run.attach(world.cluster, world.request("run-a"), labels=LedgerLabels())

    assert world.state(key) is LedgerState.WITHDRAWN
    retried = run.attach(world.cluster, world.request("run-a"), labels=LedgerLabels())
    assert isinstance(retried, Attached)
    assert retried.operation_id == ledger_operation_id(key, 1)


def test_an_attached_run_leases_its_request(tmp_path: Path) -> None:
    world = _World(tmp_path)
    run = world.attachment("run-a")
    attached = run.attach(world.cluster, world.request("run-a"), labels=LedgerLabels())
    assert isinstance(attached, Attached)
    entry = world.ledger.read(attached.key)
    assert entry is not None
    assert [lease.holder for lease in entry.leases] == ["run-a"]
    assert entry.leases[0].expires_at == world.clock.now() + LedgerPolicy().lease_seconds

    run.release(attached.key)
    entry = world.ledger.read(attached.key)
    assert entry is not None
    assert entry.leases == ()
