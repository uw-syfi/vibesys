"""The same-path contract: a path the agent names is the path the compute node sees."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.slurm_cluster.cluster import SlurmCluster
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster


def test_a_job_runs_on_the_compute_node_in_the_directory_the_agent_ran_from(
    gpu_run: OpenRun, slurm_cluster: SlurmCluster
) -> None:
    nested = gpu_run.workspace / "nested" / "dir"

    result = gpu_run.agent(
        "mkdir -p nested/dir && cd nested/dir && "
        "\"$VIBESYS_GPU\" -- sh -c 'pwd -P; hostname; echo made > made-by-job.txt'"
    )

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert str(nested) in lines
    # Not the agent container, not the host, and not the head node: the compute node.
    assert "node1" in lines
    made = nested / "made-by-job.txt"
    assert made.read_text(encoding="utf-8") == "made\n"
    assert made.stat().st_uid == os.getuid()
    assert gpu_run.agent("cat nested/dir/made-by-job.txt").output == "made\n"
    assert slurm_cluster.queue() == []


def test_a_file_the_agent_names_by_absolute_path_is_the_same_file_in_the_job(
    gpu_run: OpenRun,
) -> None:
    written = gpu_run.workspace / "from-agent.txt"

    result = gpu_run.agent(
        f"echo agent-wrote > {written} && "
        f"\"$VIBESYS_GPU\" -- sh -c 'cat {written}; echo job-appended >> {written}'"
    )

    assert result.exit_code == 0, result.output
    assert "agent-wrote" in result.output
    assert written.read_text(encoding="utf-8") == "agent-wrote\njob-appended\n"


@pytest.mark.parametrize("elsewhere", ["/home/agent", "/"])
def test_a_request_from_outside_the_workspace_is_refused_and_starts_no_job(
    run: OpenRun, slurm_cluster: SlurmCluster, elsewhere: str
) -> None:
    gate = run.agent(f"cd {elsewhere} && {run.launcher()} --gate accuracy")

    assert gate.exit_code == 2, gate.output
    assert "inside the run's workspace" in gate.output
    assert slurm_cluster.queue() == []


def test_a_gpu_request_from_a_shared_directory_that_is_not_the_workspace_is_refused(
    gpu_run: OpenRun, slurm_cluster: SlurmCluster
) -> None:
    # Visible to the compute node, so only the broker's workspace check stops it.
    result = gpu_run.agent(f'cd {slurm_cluster.root} && "$VIBESYS_GPU" -- true')

    assert result.exit_code == 2, result.output
    assert "inside the run's workspace" in result.output
