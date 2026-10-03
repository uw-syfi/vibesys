"""Contract of the executable Fake Slurm cluster used by cancellation tests."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

from vs_slurm.api import (
    SlurmConfig,
    SlurmConnectorTransport,
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
    recorded_commands,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import pytest


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
