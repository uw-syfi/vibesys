"""Contract of the executable Fake Slurm cluster used by cancellation tests."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

from vs_slurm.api import (
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
)

# test-isolation: the Fake connector is an executable test double outside the library API.
from vs_slurm.fake_connector import JOB_ID, handle, recorded_commands

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _runner(state: Path) -> SlurmJobRunner:
    def connector(
        argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        # The executable reads one request from stdin; answer it in-process.
        del timeout
        assert stdin is not None
        return subprocess.CompletedProcess(argv, 0, json.dumps(handle(state, json.loads(stdin))))

    config = SlurmConfig(
        name="fake",
        remote_workspace_root="/remote/runs",
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
