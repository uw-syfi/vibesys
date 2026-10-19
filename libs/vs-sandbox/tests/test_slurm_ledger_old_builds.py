"""A build that shares a project's ledger with an older build never makes a request unreadable to it.

An older build (main before #3173) decodes every ``attempt-*.json`` revision of a
request, and every lease, in format 1 (``ledger_format_1``), and fails the whole
request on the first document it cannot decode: its evaluations of that request
then fail, for good, since revisions are never deleted. So every document this
build leaves where the older build reads, in every state a request passes
through, must decode in format 1.

The ledger here is the one on the cluster's filesystem, so the test reads the
files themselves, as the older build lists them.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from vs_sandbox.api.slurm import Attached, SlurmLedgerAttachment
from vs_sim.api.testing import ManualClock
from vs_slurm.api import (
    ClusterCollected,
    FakeCluster,
    LedgerLabels,
    LedgerPolicy,
    RemoteEvaluationLedger,
    SecondsRange,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStage,
    SlurmBatchStageResult,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobStatus,
    SlurmTimingProfile,
    WithdrawalTiming,
    Withdrawer,
    withdraw,
)

# test-isolation: the public executable transport Fake gives the remote ledger a real filesystem.
from vs_slurm.fake_connector import FakeConnector

from .ledger_format_1 import FormatOneLease, FormatOneRecord, read_as_format_1

PROJECT = "demo-0123456789ab"
POLICY = LedgerPolicy()
_QUEUED_FOREVER = SlurmTimingProfile(queue_wait_s=SecondsRange.exactly(10**9))


class _World:
    def __init__(self, root: Path) -> None:
        self.root = root
        remote = root / "remote"
        remote.mkdir()
        self.config = SlurmConfig(
            name="contract",
            remote_workspace_root=str(remote),
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        )
        self.ledger_root = remote / "contract" / "ledger" / PROJECT
        self.connector = FakeConnector(root / "connector")
        self.scratch = root / "scratch"
        self.scratch.mkdir()
        self.clock = ManualClock(1_760_500_000.0)
        self.cluster = FakeCluster(clock=self.clock, timing=_QUEUED_FOREVER)

    def run(self, holder: str) -> SlurmLedgerAttachment:
        ledger = RemoteEvaluationLedger(
            self.config,
            project=PROJECT,
            processes=self.connector,
            clock=self.clock,
            scratch_root=self.scratch,
        )
        return SlurmLedgerAttachment(
            ledger=ledger,
            config=self.config,
            project=PROJECT,
            holder=holder,
            policy=POLICY,
            clock=self.clock,
        )

    def request(self, content: str) -> SlurmBatchRequest:
        workspace = self.root / "workspaces" / content
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "bench.py").write_text(f"print({content!r})", encoding="utf-8")
        return SlurmBatchRequest(
            workspace=workspace,
            stages=(SlurmBatchStage(name="benchmark", command=("python", "bench.py")),),
        )

    def documents_an_older_build_reads(self) -> list[Path]:
        """The files an older build's read lists: every record revision and every lease."""
        return sorted(
            (
                *self.ledger_root.glob("*/attempt-*.json"),
                *self.ledger_root.glob("*/leases/*.json"),
            )
        )

    def assert_an_older_build_reads_everything(self, after: str) -> None:
        documents = self.documents_an_older_build_reads()
        assert documents, "the scenario wrote nothing an older build reads"
        for path in documents:
            model = FormatOneLease if path.parent.name == "leases" else FormatOneRecord
            try:
                read_as_format_1(path.read_text(encoding="utf-8"), model)
            except ValueError as error:
                message = f"after {after}, an older build cannot read {path.name}: {error}"
                raise AssertionError(message) from error


def _attached(
    run: SlurmLedgerAttachment, cluster: FakeCluster, request: SlurmBatchRequest
) -> Attached:
    outcome = run.attach(cluster, request, labels=LedgerLabels(candidate="0f3c9e1"))
    assert isinstance(outcome, Attached), outcome
    return outcome


def _collected(operation_id: str) -> ClusterCollected:
    return ClusterCollected(
        operation_id=operation_id,
        result=SlurmBatchResult(
            job_id="1",
            job_exit_code=0,
            job_output="",
            stages=(
                SlurmBatchStageResult(
                    name="benchmark",
                    exit_code=0,
                    stdout="ok",
                    stderr="",
                    elapsed_seconds=1.0,
                    skipped=False,
                ),
            ),
            phase_timings_seconds={},
            content_cache_hits=0,
        ),
    )


@pytest.mark.regression
def test_every_record_and_lease_this_build_writes_reads_in_format_1() -> None:
    """r3173 HIGH-1: one record an older build cannot decode failed its evaluations of the request.

    Every way this build writes the ledger runs here: a claim and its job, a
    second run attaching, a relinquish another run keeps, one that withdraws and
    cancels, a completed and a failed attempt, the sweep, and an operator's
    removal of a completed entry. After each, every document an older build
    reads decodes in format 1.
    """
    with TemporaryDirectory() as directory:
        world = _World(Path(directory))
        first, second, sweeper = world.run("run-1"), world.run("run-2"), world.run("run-3")
        shared = world.request("shared")
        key = first.key(shared)

        _attached(first, world.cluster, shared)
        world.assert_an_older_build_reads_everything("a claim and its job")
        _attached(second, world.cluster, shared)
        world.assert_an_older_build_reads_everything("a second run attached")
        first.relinquish(world.cluster, key)
        world.assert_an_older_build_reads_everything("a relinquish another run keeps")
        second.relinquish(world.cluster, key)
        world.assert_an_older_build_reads_everything("a relinquish that withdraws")

        again = _attached(first, world.cluster, shared)
        first.settle(
            key, again.operation_id, SlurmJobStatus.COMPLETED, _collected(again.operation_id)
        )
        world.assert_an_older_build_reads_everything("a completed attempt")

        failing = world.request("failing")
        failed = _attached(second, world.cluster, failing)
        second.settle(failed.key, failed.operation_id, SlurmJobStatus.FAILED, None)
        world.assert_an_older_build_reads_everything("a failed attempt")

        abandoned = world.request("abandoned")
        _attached(second, world.cluster, abandoned)
        world.clock.advance(POLICY.lease_seconds + POLICY.grace_seconds + 1)
        sweeper.sweep_if_due(world.cluster)
        world.assert_an_older_build_reads_everything("the sweep")

        withdraw(
            world.run("vibesys-evals").ledger,
            world.cluster,
            key,
            Withdrawer(
                by="vibesys-evals",
                reason="removed by operator",
                timing=WithdrawalTiming(
                    clock=world.clock, call_seconds=world.config.transport_timeout_seconds
                ),
            ),
        )
        world.assert_an_older_build_reads_everything("a removal of a completed entry")
        # The withdrawals did mark their attempts, where an older build never looks.
        assert list(world.ledger_root.glob("*/withdrawal-*.json"))
