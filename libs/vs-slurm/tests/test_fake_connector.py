"""Contract of the executable Fake Slurm cluster used by cancellation tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitted,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmFileArtifact,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmSshTransport,
)

# test-isolation: the Fake connector is an executable test double outside the library API.
from vs_slurm.fake_connector import (
    HOLD_FILE,
    JOB_ID,
    executing_cluster,
    handle,
    main,
    pending_jobs,
    recorded_commands,
    release_job,
    release_jobs,
)

# test-isolation: public wiring constructs the production implementation for transport contracts.
from vs_slurm.wiring import SlurmCluster

if TYPE_CHECKING:
    from collections.abc import Sequence


def _runner(state: Path, remote_root: str = "/remote/runs") -> SlurmJobRunner:
    def connector(
        argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        # The executable reads one request from stdin; answer it in-process.
        del timeout
        assert stdin is not None
        return subprocess.CompletedProcess(argv, 0, json.dumps(handle(state, json.loads(stdin))))

    config = SlurmConfig(
        name="fake",
        remote_workspace_root=remote_root,
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
    )
    return SlurmJobRunner(config, process=connector)


def test_a_submitted_job_stays_pending_until_it_is_cancelled(tmp_path: Path) -> None:
    state = tmp_path / "cluster"
    state.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = _runner(state)

    job = runner.submit(SlurmJobRequest(workspace=workspace, command=("run-benchmark",)))

    assert job.job_id == JOB_ID
    assert runner.poll(job) is SlurmJobStatus.PENDING
    assert runner.poll(job) is SlurmJobStatus.PENDING
    runner.cancel(job)
    assert runner.poll(job) is SlurmJobStatus.CANCELLED
    assert recorded_commands(state).count(f"scancel {JOB_ID}") == 1


def test_no_requests_are_recorded_before_the_first_one(tmp_path: Path) -> None:
    assert recorded_commands(tmp_path) == []


def _executing_runner(tmp_path: Path) -> tuple[Path, SlurmJobRunner, Path]:
    state = executing_cluster(tmp_path / "cluster")
    remote = tmp_path / "remote"
    remote.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("staged", encoding="utf-8")
    return state, _runner(state, str(remote)), workspace


def test_an_executing_cluster_runs_the_staged_job_before_its_first_poll(tmp_path: Path) -> None:
    _state, runner, workspace = _executing_runner(tmp_path)
    request = SlurmJobRequest(workspace=workspace, command=("cat", "input.txt"))

    job = runner.submit(request)
    assert runner.poll(job) is SlurmJobStatus.COMPLETED
    failed = runner.run(SlurmJobRequest(workspace=workspace, command=("false",)))

    assert failed.exit_code != 0
    assert failed.job_id != job.job_id
    assert "staged" in runner.run(request).output


def test_an_executing_cluster_holds_jobs_until_they_are_cancelled(tmp_path: Path) -> None:
    state, runner, workspace = _executing_runner(tmp_path)
    (state / HOLD_FILE).touch()

    job = runner.submit(SlurmJobRequest(workspace=workspace, command=("true",)))

    assert runner.poll(job) is SlurmJobStatus.PENDING
    runner.cancel(job)
    assert runner.poll(job) is SlurmJobStatus.CANCELLED
    assert recorded_commands(state).count(f"scancel {job.job_id}") == 1


def test_the_ssh_stand_in_answers_like_the_connector(tmp_path: Path) -> None:
    state = tmp_path / "cluster"
    state.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    program = (sys.executable, "-m", "vs_slurm.fake_connector", str(state))
    runner = SlurmJobRunner(
        SlurmConfig(
            name="fake",
            remote_workspace_root="/remote/runs",
            transport=SlurmSshTransport(
                host="fake", ssh_command=(*program, "ssh"), rsync_command=(*program, "rsync")
            ),
        )
    )

    job = runner.submit(SlurmJobRequest(workspace=workspace, command=("run-benchmark",)))

    assert job.job_id == JOB_ID
    assert runner.poll(job) is SlurmJobStatus.PENDING
    runner.cancel(job)
    assert runner.poll(job) is SlurmJobStatus.CANCELLED
    assert recorded_commands(state).count(f"scancel {JOB_ID}") == 1


def test_the_ssh_and_rsync_stand_ins_answer_in_process(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "cluster"
    state.mkdir()

    assert main([str(state), "rsync", "-a", "--", "local/", "fake:/remote/runs/x/"]) == 0
    assert main([str(state), "ssh", "--", "fake", f"squeue -h -j {JOB_ID} -o %T"]) == 0
    assert capsys.readouterr().out == "PENDING\n"
    assert main([str(state), "ssh", "--", "fake", f"scancel {JOB_ID}"]) == 0
    assert main([str(state), "ssh", "--", "fake", f"squeue -h -j {JOB_ID} -o %T"]) == 0

    assert capsys.readouterr().out == ""
    assert recorded_commands(state) == [
        f"squeue -h -j {JOB_ID} -o %T",
        f"scancel {JOB_ID}",
        f"squeue -h -j {JOB_ID} -o %T",
    ]


def test_batch_collection_preserves_completed_stage_artifacts_without_allocation_status(
    tmp_path: Path,
) -> None:
    """A node failure after a completed stage cannot erase that stage's evidence."""
    _state, runner, workspace = _executing_runner(tmp_path)
    artifact = tmp_path / "collected" / "evidence.txt"
    batch = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace,
            stages=(
                SlurmBatchStage(
                    name="completed",
                    command=("bash", "-c", "printf evidence > evidence.txt"),
                    file_artifacts=(SlurmFileArtifact("evidence.txt", artifact),),
                ),
            ),
        )
    )
    Path(batch.job.remote_status_path).unlink()

    result = runner.collect_batch(batch)

    assert result.job_exit_code is None
    assert result.collection_failure
    assert len(result.stages) == 1
    assert result.stages[0].exit_code == 0
    assert result.stages[0].artifacts[0].local_path == artifact
    assert artifact.read_text(encoding="utf-8") == "evidence"


def test_pending_transport_retains_remote_operation_identity_across_local_caches(
    tmp_path: Path,
) -> None:
    state = tmp_path / "cluster"
    state.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = _runner(state)
    first = SlurmCluster(runner, state_root=tmp_path / "first-cache")
    second = SlurmCluster(runner, state_root=tmp_path / "second-cache")
    request = SlurmJobRequest(workspace=workspace, command=("benchmark",))

    submitted = first.submit(request, operation_id="pending")
    assert isinstance(submitted, ClusterSubmitted)
    duplicate = second.submit(request, operation_id="pending")
    assert isinstance(duplicate, ClusterSubmitted)
    assert isinstance(duplicate.handle, SlurmJobHandle)
    assert isinstance(submitted.handle, SlurmJobHandle)
    assert duplicate.handle.job_id == submitted.handle.job_id
    first.cancel("pending")
    observed = second.inspect("pending")
    assert isinstance(observed, ClusterObservation)
    assert observed.status is SlurmJobStatus.CANCELLED
    assert len([command for command in recorded_commands(state) if "&& sbatch " in command]) == 1


def test_pending_transport_cancels_only_the_selected_job(tmp_path: Path) -> None:
    state = tmp_path / "cluster"
    state.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = _runner(state)
    request = SlurmJobRequest(workspace=workspace, command=("benchmark",))
    first = runner.submit(request)
    second = runner.submit(request)
    assert first.job_id != second.job_id
    runner.cancel(first)
    assert runner.poll(first) is SlurmJobStatus.CANCELLED
    assert runner.poll(second) is SlurmJobStatus.PENDING


@pytest.mark.parametrize("execute", [False, True])
def test_ssh_cluster_preserves_operation_identity_with_both_fake_modes(
    tmp_path: Path, *, execute: bool
) -> None:
    state = tmp_path / "cluster"
    state.mkdir()
    if execute:
        executing_cluster(state)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    remote = tmp_path / "remote"
    remote.mkdir()
    program = (sys.executable, "-m", "vs_slurm.fake_connector", str(state))
    runner = SlurmJobRunner(
        SlurmConfig(
            name="fake",
            remote_workspace_root=str(remote) if execute else "/remote/runs",
            transport=SlurmSshTransport(
                host="fake", ssh_command=(*program, "ssh"), rsync_command=(*program, "rsync")
            ),
        )
    )
    first = SlurmCluster(runner, state_root=tmp_path / "first-cache")
    second = SlurmCluster(runner, state_root=tmp_path / "second-cache")
    request = SlurmJobRequest(workspace=workspace, command=("true",))

    submitted = first.submit(request, operation_id="ssh-operation")
    assert isinstance(submitted, ClusterSubmitted)
    duplicate = second.submit(request, operation_id="ssh-operation")
    assert isinstance(duplicate, ClusterSubmitted)
    assert isinstance(submitted.handle, SlurmJobHandle)
    assert isinstance(duplicate.handle, SlurmJobHandle)
    assert duplicate.handle.job_id == submitted.handle.job_id
    observed = second.inspect("ssh-operation")
    assert isinstance(observed, ClusterObservation)
    assert observed.status is (SlurmJobStatus.COMPLETED if execute else SlurmJobStatus.PENDING)


@pytest.mark.parametrize("exit_code", [0, 3])
def test_held_allocation_runs_its_retained_script_once_when_released(
    tmp_path: Path, exit_code: int
) -> None:
    state, runner, workspace = _executing_runner(tmp_path)
    (state / HOLD_FILE).touch()
    output = tmp_path / "allocation.txt"
    request = SlurmJobRequest(
        workspace=workspace,
        command=("bash", "-c", f'echo "$SLURM_JOB_ID" >> {output}; exit {exit_code}'),
    )
    job = runner.submit(request)
    assert pending_jobs(state) == (job.job_id,)
    assert runner.poll(job) is SlurmJobStatus.PENDING
    assert not output.exists()

    release_job(state, job.job_id)
    release_job(state, job.job_id)

    assert runner.poll(job) is (
        SlurmJobStatus.COMPLETED if exit_code == 0 else SlurmJobStatus.FAILED
    )
    assert output.read_text(encoding="utf-8") == f"{job.job_id}\n"
    assert pending_jobs(state) == ()
    assert len([command for command in recorded_commands(state) if "&& sbatch " in command]) == 1


def test_releasing_held_allocations_never_restarts_a_cancelled_job(tmp_path: Path) -> None:
    state, runner, workspace = _executing_runner(tmp_path)
    (state / HOLD_FILE).touch()
    request = SlurmJobRequest(workspace=workspace, command=("true",))
    cancelled = runner.submit(request)
    held = runner.submit(request)
    runner.cancel(cancelled)

    release_job(state, cancelled.job_id)
    release_jobs(state)

    assert runner.poll(cancelled) is SlurmJobStatus.CANCELLED
    assert runner.poll(held) is SlurmJobStatus.COMPLETED
    assert pending_jobs(state) == ()
    subsequent = runner.submit(request)
    assert runner.poll(subsequent) is SlurmJobStatus.COMPLETED
