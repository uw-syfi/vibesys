"""Stopping the container or closing the session leaves no jobs, brokers, sockets or containers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.cluster import HANG_GUARD_SECONDS, docker
from tests.slurm_cluster.harness import (
    ALL_KINDS,
    in_background,
    open_run,
    run_container_ids,
)

if TYPE_CHECKING:
    from tests.slurm_cluster.cluster import SlurmCluster
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster

_HOLD = '"$VIBESYS_GPU" -- sleep 600'
#: The kind whose held job is stopped. The deprecated alias translates to the same
#: environment, so its close is the same code; opening it (below) proves its broker.
_HOLDING_KIND = "slurm-local-gpu"


def _broker_sockets(run: OpenRun) -> list[Path]:
    """Every host socket the run exposed: the container's broker, and the SSH transport's, if any."""
    inspected = docker("inspect", "--format", "{{json .Config.Env}}", run.container_id)
    env = dict(entry.split("=", 1) for entry in json.loads(inspected.stdout))
    sockets = [Path(env["VIBESYS_COMMAND_BROKER_SOCKET"])] if run.has_gpu_commands else []
    sockets += [
        Path(v) for k, v in run.session.view.profiler_mcp_env if k == "VIBESYS_SLURM_BROKER_SOCKET"
    ]
    return sockets


def test_stopping_the_agent_container_cancels_its_running_job(
    slurm_cluster: SlurmCluster, workdir: Path, agent_image_id: str
) -> None:
    with open_run(_HOLDING_KIND, slurm_cluster, workdir, agent_image_id) as run:
        pending = in_background(lambda: run.agent(_HOLD))
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


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_closing_the_session_cancels_jobs_and_removes_brokers_sockets_and_containers(
    kind: str, slurm_cluster: SlurmCluster, workdir: Path, agent_image_id: str
) -> None:
    with open_run(kind, slurm_cluster, workdir, agent_image_id) as run:
        sockets = _broker_sockets(run)
        # The agent's GPU-command broker exists only with agent GPU commands, and the transport
        # broker only for SSH: the local transport has none.
        assert len(sockets) == int(run.has_gpu_commands) + int(kind == "slurm")
        assert all(socket.exists() for socket in sockets)
        assert run_container_ids(run.run_id)
        holds_a_job = kind == _HOLDING_KIND
        pending = in_background(lambda: run.agent(_HOLD)) if holds_a_job else None
        job = slurm_cluster.wait_for_job("") if pending is not None else None

        run.session.close()

        assert slurm_cluster.queue() == []
        if job is not None and pending is not None:
            assert slurm_cluster.job_state(job) == "CANCELLED"
            assert pending.result(timeout=HANG_GUARD_SECONDS).exit_code not in (0, None)
        assert not any(socket.exists() for socket in sockets)
        assert run_container_ids(run.run_id) == []
