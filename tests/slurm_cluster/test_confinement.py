"""``JobConfinement`` confines a brokered job on the compute node (bubblewrap, for real)."""

from __future__ import annotations

import os
import secrets
from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.harness import (
    CONTAINER_TMP,
    OPERATOR_INTERPRETER_OUTPUT,
    OPERATOR_TOOL_FILE,
    OPERATOR_TOOL_TEXT,
    OPERATOR_VENV_PYTHON,
    operator_tools,
)

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
    result = gpu_run.agent(f'"$VIBESYS_GPU" -- ls -1 {slurm_cluster.root}')

    assert result.exit_code == 0, result.output
    visible = result.output.splitlines()[1:]  # after the launcher's header line
    # Only the workspace, and the read-only directory the operator exposed on purpose.
    assert visible == sorted([gpu_run.workspace.name, operator_tools(slurm_cluster).name])


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


def test_a_job_reads_the_operators_read_only_tools_outside_its_workspace_and_cannot_write_them(
    gpu_run: OpenRun, slurm_cluster: SlurmCluster
) -> None:
    """Regression: an interpreter outside the workspace was invisible to the job (exit 127)."""
    tool = operator_tools(slurm_cluster) / OPERATOR_TOOL_FILE
    other = slurm_cluster.root / "not-exposed"
    other.mkdir(exist_ok=True)
    (other / "secret.txt").write_text("hidden\n", encoding="utf-8")

    read = gpu_run.agent(f'"$VIBESYS_GPU" -- cat {tool}')
    write = gpu_run.agent(f"\"$VIBESYS_GPU\" -- sh -c 'echo x > {tool}'")
    hidden = gpu_run.agent(f'"$VIBESYS_GPU" -- cat {other}/secret.txt')

    assert read.exit_code == 0, read.output
    assert read.output.endswith(OPERATOR_TOOL_TEXT)
    assert write.exit_code != 0
    assert tool.read_text(encoding="utf-8") == OPERATOR_TOOL_TEXT
    assert hidden.exit_code != 0


def test_a_job_runs_an_interpreter_reached_through_a_chain_of_links_inside_the_listed_path(
    gpu_run: OpenRun, slurm_cluster: SlurmCluster
) -> None:
    """The venv's python links to an alias directory that links to the install; all are listed."""
    python = operator_tools(slurm_cluster) / OPERATOR_VENV_PYTHON

    result = gpu_run.agent(f'"$VIBESYS_GPU" -- {python}')

    assert result.exit_code == 0, result.output
    assert result.output.endswith(OPERATOR_INTERPRETER_OUTPUT)
