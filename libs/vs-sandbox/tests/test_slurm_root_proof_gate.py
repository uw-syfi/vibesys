"""Slurm work waits for the run's workspace-root proof; a failed proof refuses it.

Regression: the run's startup waited for the workspace-root probe instead. With
the probe beside the run, the first trusted evaluation and every agent GPU job
are what wait for it, and nothing reaches the scheduler before the root is proven.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import vs_evaluation.api.testing as evaluation_testing
from vs_evaluation.api import (
    EvaluationCoordinator,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    loop_evaluation_deadline,
)
from vs_sandbox.api.slurm import (
    CANCELLED_BEFORE_SUBMISSION,
    GpuCommand,
    GpuJobOutcome,
    GpuJobRequest,
    RootGatedGpuLauncher,
    SlurmEvaluationExecutor,
    SlurmStagePayload,
)
from vs_sim.api.testing import ManualClock, SimThreads, VirtualClock, run_virtual
from vs_slurm.api import (
    ClusterSubmitOutcome,
    FakeCluster,
    FakeConnector,
    SlurmBatchRequest,
    SlurmConfig,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmLocalTransport,
    SlurmSshTransport,
    WorkspaceRootProof,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Event

_RUN_ID = "run-on-a-full-cluster"
# test-isolation: the probe's scheduler identity, which the Fake scripts by operation id.
_PROBE_OPERATION = "workspace-root-probe-" + hashlib.sha256(_RUN_ID.encode()).hexdigest()[:32]
_QUEUE_SECONDS = 1800
_POLL_SECONDS = 10.0
# Guards against a hang only; every scenario settles within the probe's own bounds.
_VIRTUAL_LIMIT_SECONDS = 100_000.0
_TERMINAL = {EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED}


def _proof(root: Path, threads: SimThreads, *, scheduled: bool) -> WorkspaceRootProof:
    """A proof whose probe job runs at once, or waits in the queue past its deadline."""
    remote = root / "remote"
    remote.mkdir(exist_ok=True)
    connector = FakeConnector(root / "probe-cluster")
    if not scheduled:
        connector.script(
            _PROBE_OPERATION, states=(SlurmJobStatus.PENDING,), pending_reason="Resources"
        )
    runner = SlurmJobRunner(
        SlurmConfig(
            name="busy",
            remote_workspace_root=str(remote),
            transport=SlurmLocalTransport(
                kind="local",
                shell_command=("fake-connector", "shell"),
                rsync_command=("fake-connector", "rsync"),
            ),
            poll_interval_seconds=_POLL_SECONDS,
            queue_timeout_seconds=_QUEUE_SECONDS,
        ),
        processes=connector,
        scratch_root=root / "probe-state",
        clock=threads.clock,
    )
    return WorkspaceRootProof(
        runner, state_root=root / "probe-state", run_id=_RUN_ID, threads=threads
    )


class _RecordingCluster(FakeCluster):
    """The evaluation's scheduler; it records every batch it is sent."""

    def __init__(self) -> None:
        super().__init__(clock=ManualClock())
        self.submissions: list[str] = []

    def submit(
        self, request: SlurmJobRequest | SlurmBatchRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        if isinstance(request, SlurmBatchRequest):
            self.submissions.append(operation_id)
        return super().submit(request, operation_id=operation_id)


def _evaluation() -> EvaluationRequest:
    return EvaluationRequest(
        key="gated",
        stages=(
            EvaluationStep(
                name="accuracy",
                payload=SlurmStagePayload(command="run-accuracy", timeout_seconds=5).model_dump(
                    mode="json"
                ),
            ),
        ),
    )


def _executor(
    root: Path, cluster: FakeCluster, clock: VirtualClock, proof: WorkspaceRootProof
) -> SlurmEvaluationExecutor:
    workspace = root / "workspace"
    workspace.mkdir()
    return SlurmEvaluationExecutor(
        SlurmConfig(
            name="evaluations",
            remote_workspace_root="/runs",
            transport=SlurmSshTransport(host="evaluations"),
            poll_interval_seconds=_POLL_SECONDS,
        ),
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=root / "handles",
        cluster=cluster,
        sleeper=clock,
        clock=ManualClock(),
        root_proof=proof,
    )


def test_a_first_evaluation_waits_for_a_queued_probe_and_is_refused_with_its_error(
    tmp_path: Path,
) -> None:
    threads = SimThreads(VirtualClock(limit=_VIRTUAL_LIMIT_SECONDS))
    proof = _proof(tmp_path, threads, scheduled=False)
    cluster = _RecordingCluster()
    executor = _executor(tmp_path, cluster, threads.clock, proof)
    coordinator = EvaluationCoordinator(
        executor,
        evaluation_testing.InMemoryEvaluationStore(),
        ManualClock(),
        deadline_factory=loop_evaluation_deadline,
    )

    async def main() -> tuple[list[str], EvaluationState, str | None]:
        proof.start()
        handle = await coordinator.submit(_evaluation())
        await threads.clock.sleep(_QUEUE_SECONDS / 2)
        sent_while_waiting = list(cluster.submissions)
        record = await coordinator.snapshot(handle.id)
        while record.state not in _TERMINAL:
            await threads.clock.sleep(_POLL_SECONDS)
            record = await coordinator.snapshot(handle.id)
        await executor.close()
        proof.close()
        return sent_while_waiting, record.state, record.failure

    sent_while_waiting, state, failure = run_virtual(threads.clock, main(), driver=threads)

    assert sent_while_waiting == []
    assert state is EvaluationState.FAILED
    assert failure is not None
    assert "probe job 5000 was never scheduled" in failure
    assert cluster.submissions == []


def test_an_evaluation_reaches_the_scheduler_once_the_root_is_proven(tmp_path: Path) -> None:
    threads = SimThreads(VirtualClock(limit=_VIRTUAL_LIMIT_SECONDS))
    proof = _proof(tmp_path, threads, scheduled=True)
    cluster = _RecordingCluster()
    executor = _executor(tmp_path, cluster, threads.clock, proof)
    coordinator = EvaluationCoordinator(
        executor,
        evaluation_testing.InMemoryEvaluationStore(),
        ManualClock(),
        deadline_factory=loop_evaluation_deadline,
    )

    async def main() -> list[str]:
        proof.start()
        await coordinator.submit(_evaluation())
        while not cluster.submissions:
            await threads.clock.sleep(_POLL_SECONDS)
        await executor.close()
        proof.close()
        return list(cluster.submissions)

    assert len(run_virtual(threads.clock, main(), driver=threads)) == 1


class _CountingLauncher:
    """Fake ``srun``: records each command it is handed and exits at once."""

    def __init__(self) -> None:
        self.submitted: list[tuple[str, ...]] = []

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> GpuJobOutcome:
        del request, write, cancel
        self.submitted.append(command.argv)
        return GpuJobOutcome(0)


_GPU_REQUEST = GpuJobRequest(gpus=1, time_minutes=10)
_GPU_COMMAND = GpuCommand(argv=("nvidia-smi",), cwd=Path("workspace"), env={})


def test_an_agent_gpu_job_waits_for_the_proof_and_is_refused_once_it_failed(
    tmp_path: Path,
) -> None:
    threads = SimThreads(VirtualClock(limit=_VIRTUAL_LIMIT_SECONDS))
    proof = _proof(tmp_path, threads, scheduled=False)
    inner = _CountingLauncher()
    launcher = RootGatedGpuLauncher(inner, proof)

    def run() -> tuple[GpuJobOutcome, GpuJobOutcome]:
        proof.start()
        cancel = threads.event()
        threads.spawn(lambda: (threads.sleep(60.0), cancel.set()), name="agent-stops")
        stopped = launcher.run(_GPU_REQUEST, _GPU_COMMAND, write=lambda _chunk: None, cancel=cancel)
        refused = launcher.run(
            _GPU_REQUEST, _GPU_COMMAND, write=lambda _chunk: None, cancel=threads.event()
        )
        proof.close()
        return stopped, refused

    try:
        stopped, refused = threads.run(run)
    finally:
        threads.close()

    assert stopped.exit_code == CANCELLED_BEFORE_SUBMISSION
    assert refused.exit_code != 0
    assert "probe job 5000 was never scheduled" in str(refused.failure)
    assert inner.submitted == []


def test_an_agent_gpu_job_runs_once_the_root_is_proven(tmp_path: Path) -> None:
    threads = SimThreads(VirtualClock(limit=_VIRTUAL_LIMIT_SECONDS))
    proof = _proof(tmp_path, threads, scheduled=True)
    inner = _CountingLauncher()
    launcher = RootGatedGpuLauncher(inner, proof)

    def run() -> GpuJobOutcome:
        proof.start()
        outcome = launcher.run(
            _GPU_REQUEST, _GPU_COMMAND, write=lambda _chunk: None, cancel=threads.event()
        )
        proof.close()
        return outcome

    try:
        outcome = threads.run(run)
    finally:
        threads.close()

    assert outcome.exit_code == 0
    assert inner.submitted == [("nvidia-smi",)]
