"""Parallel candidates of one run each get a broker that serves only their own worktree."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.cluster import docker
from tests.slurm_cluster.harness import open_candidates

if TYPE_CHECKING:
    from pathlib import Path

    from tests.slurm_cluster.cluster import SlurmCluster
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster

_COUNT = 2


def _broker_env(candidate: OpenRun) -> tuple[str, str]:
    """The socket and token of the broker the candidate's container reaches."""
    inspected = docker("inspect", "--format", "{{json .Config.Env}}", candidate.container_id)
    env = dict(entry.split("=", 1) for entry in json.loads(inspected.stdout))
    return env["VIBESYS_COMMAND_BROKER_SOCKET"], env["VIBESYS_COMMAND_BROKER_TOKEN"]


# What an agent that does not use the launcher sends: its own request to the broker it holds
# the socket and token of, naming any working directory. Prints the broker's reply lines.
_RAW_REQUEST = """\
import json, os, socket, sys
cwd = sys.argv[1]
call = {"op": "gpu", "token": os.environ["VIBESYS_COMMAND_BROKER_TOKEN"], "cwd": cwd,
        "argv": ["touch", "intruder"], "env": {}}
wire = socket.socket(socket.AF_UNIX)
wire.connect(os.environ["VIBESYS_COMMAND_BROKER_SOCKET"])
wire.sendall(json.dumps(call).encode() + b"\\n")
print(wire.makefile().read())
"""


def test_each_candidates_gpu_command_runs_on_a_node_in_its_own_worktree(
    slurm_cluster: SlurmCluster, workdir: Path, agent_image_id: str
) -> None:
    with open_candidates(
        "slurm-local-gpu", slurm_cluster, workdir, agent_image_id, count=_COUNT
    ) as candidates:
        for candidate in candidates:
            result = candidate.agent("\"$VIBESYS_GPU\" -- sh -c 'hostname; pwd'")

            assert result.exit_code == 0, result.output
            lines = result.output.splitlines()
            assert "node1" in lines
            assert str(candidate.workspace) in lines


def test_a_candidates_gpu_command_is_refused_in_the_other_candidates_worktree(
    slurm_cluster: SlurmCluster, workdir: Path, agent_image_id: str
) -> None:
    with open_candidates(
        "slurm-local-gpu", slurm_cluster, workdir, agent_image_id, count=_COUNT
    ) as candidates:
        first, second = candidates

        # The container mounts only its own worktree, so the launcher cannot even start in the
        # other's; a request written by hand can name it, and the broker must refuse by name.
        refused = first.agent(f"python3 -c '{_RAW_REQUEST}' {second.workspace}")
        allowed = first.agent(f"python3 -c '{_RAW_REQUEST}' {first.workspace}")

        assert "inside the run's workspace" in refused.output
        assert "error" not in allowed.output
        slurm_cluster.wait_for_empty_queue()
        assert not (second.workspace / "intruder").exists()
        assert (first.workspace / "intruder").exists()


def test_the_candidates_brokers_have_distinct_sockets_and_tokens(
    slurm_cluster: SlurmCluster, workdir: Path, agent_image_id: str
) -> None:
    with open_candidates(
        "slurm-local-gpu", slurm_cluster, workdir, agent_image_id, count=_COUNT
    ) as candidates:
        sockets, tokens = zip(*(_broker_env(candidate) for candidate in candidates), strict=True)

        assert len(set(sockets)) == _COUNT
        assert len(set(tokens)) == _COUNT
