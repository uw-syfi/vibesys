"""``JobConfinement`` confines a brokered job on the compute node (bubblewrap, for real)."""

from __future__ import annotations

import os
import secrets
from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.harness import CONTAINER_TMP

if TYPE_CHECKING:
    from tests.slurm_cluster.cluster import SlurmCluster
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster


def test_a_write_outside_the_workspace_never_reaches_the_shared_filesystem(
    gpu_run: OpenRun, slurm_cluster: SlurmCluster
) -> None:
    outside = slurm_cluster.root / f"outside-{secrets.token_hex(4)}.txt"
    inside = gpu_run.workspace / "inside.txt"
    # Control: the same user, on the same node, with no confinement, can write there.
    control = slurm_cluster.slurm("srun", "-n1", "touch", str(outside), check=False)
    assert control.returncode == 0, control.stderr
    assert outside.exists()
    outside.unlink()

    # The write "succeeds" in the job's own scratch root, which nothing else sees.
    result = gpu_run.agent(
        f"\"$VIBESYS_GPU\" -- sh -c 'echo x > {outside}; echo x > {inside}; cat {outside}'"
    )

    assert result.exit_code == 0, result.output
    assert not outside.exists()
    assert (
        slurm_cluster.run(slurm_cluster.node, ("test", "-e", str(outside)), check=False).returncode
        == 1
    )
    # What the workspace allows is not blocked.
    assert inside.read_text(encoding="utf-8") == "x\n"


def test_a_job_sees_its_workspace_and_none_of_the_shared_directory_around_it(
    gpu_run: OpenRun, slurm_cluster: SlurmCluster
) -> None:
    result = gpu_run.agent(f'"$VIBESYS_GPU" -- ls {slurm_cluster.root}')

    assert result.exit_code == 0, result.output
    visible = result.output.splitlines()[1:]  # after the launcher's header line
    assert visible == [gpu_run.workspace.name]


def test_a_job_writes_to_a_private_tmp_that_the_node_never_sees(
    gpu_run: OpenRun, slurm_cluster: SlurmCluster
) -> None:
    marker = f"{CONTAINER_TMP}/vibesys-confinement-{secrets.token_hex(4)}"

    result = gpu_run.agent(f"\"$VIBESYS_GPU\" -- sh -c 'echo x > {marker} && cat {marker}'")

    assert result.exit_code == 0, result.output
    assert result.output.endswith("x\n")
    assert (
        slurm_cluster.run(slurm_cluster.node, ("test", "-e", marker), check=False).returncode == 1
    )


def test_a_job_runs_as_the_unprivileged_host_user_in_its_own_pid_namespace(
    gpu_run: OpenRun,
) -> None:
    result = gpu_run.agent("\"$VIBESYS_GPU\" -- sh -c 'id -u; ps -e -o pid= | wc -l'")

    assert result.exit_code == 0, result.output
    uid, processes = result.output.splitlines()[1:3]
    assert int(uid) == os.getuid()
    # Only the confinement's own few processes, not the node's.
    assert int(processes) < 10
