"""Stopping the container or closing the session leaves no jobs, brokers, sockets or containers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.cluster import HANG_GUARD_SECONDS, docker
from tests.slurm_cluster.harness import in_background, open_run, run_container_ids

if TYPE_CHECKING:
    from tests.slurm_cluster.cluster import SlurmCluster
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster


def _hold(run: OpenRun) -> str:
    if run.session.view.env_kind == "slurm-gpu":
        return '"$VIBESYS_GPU" -- sleep 600'
    run.set_mode("accuracy", "hold")
    return f"{run.launcher()} --gate accuracy"


def _broker_sockets(run: OpenRun) -> list[Path]:
    """Every host socket the run exposed: the container's broker, and the SSH transport's, if any."""
    inspected = docker("inspect", "--format", "{{json .Config.Env}}", run.container_id)
    env = dict(entry.split("=", 1) for entry in json.loads(inspected.stdout))
    sockets = [Path(env["VIBESYS_COMMAND_BROKER_SOCKET"])]
    sockets += [
        Path(v) for k, v in run.session.view.profiler_mcp_env if k == "VIBESYS_SLURM_BROKER_SOCKET"
    ]
    return sockets


@pytest.mark.parametrize("kind", ["slurm", "slurm-local", "slurm-gpu"])
def test_stopping_the_agent_container_cancels_its_running_job(
    kind: str, slurm_cluster: SlurmCluster, workdir: Path, agent_image_id: str
) -> None:
    with open_run(kind, slurm_cluster, workdir, agent_image_id) as run:
        pending = in_background(lambda: run.agent(_hold(run)))
        job = slurm_cluster.wait_for_job("")

        docker("kill", run.container_id)
        result = pending.result(timeout=HANG_GUARD_SECONDS)

        # The brokers are still up: the container's end alone cancelled the job.
        assert result.exit_code not in (0, None)
        slurm_cluster.wait_for_empty_queue()
        assert slurm_cluster.job_state(job) == "CANCELLED"
        assert all(socket.exists() for socket in _broker_sockets_after_kill(run))


def _broker_sockets_after_kill(run: OpenRun) -> list[Path]:
    # ``docker inspect`` still answers for a stopped container.
    return _broker_sockets(run)


@pytest.mark.parametrize("kind", ["slurm", "slurm-local", "slurm-gpu"])
def test_closing_the_session_cancels_jobs_and_removes_brokers_sockets_and_containers(
    kind: str, slurm_cluster: SlurmCluster, workdir: Path, agent_image_id: str
) -> None:
    with open_run(kind, slurm_cluster, workdir, agent_image_id) as run:
        sockets = _broker_sockets(run)
        # The transport broker exists only for SSH: the local transport has none.
        assert len(sockets) == {"slurm": 2, "slurm-local": 1, "slurm-gpu": 1}[kind]
        assert all(socket.exists() for socket in sockets)
        assert run_container_ids(run.run_id)
        pending = in_background(lambda: run.agent(_hold(run)))
        job = slurm_cluster.wait_for_job("")

        run.session.close()

        assert slurm_cluster.queue() == []
        assert slurm_cluster.job_state(job) == "CANCELLED"
        assert not any(socket.exists() for socket in sockets)
        assert run_container_ids(run.run_id) == []
        assert pending.result(timeout=HANG_GUARD_SECONDS).exit_code not in (0, None)
