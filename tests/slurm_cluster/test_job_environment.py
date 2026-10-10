"""Jobs run with a sanitized environment: no host secrets, no container identity, no Slurm state."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.conftest import (
    HOST_CANARY_ENV,
    HOST_CANARY_VALUE,
    expect_failure_for,
)
from tests.slurm_cluster.harness import open_run, parse_env

if TYPE_CHECKING:
    from pathlib import Path

    from tests.slurm_cluster.cluster import SlurmCluster
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster


def test_a_gpu_job_gets_host_basics_and_the_agents_variables_but_not_the_containers_identity(
    gpu_run: OpenRun,
) -> None:
    container = parse_env(gpu_run.agent("env").output)
    assert container["HOME"] == "/home/agent"  # the identity that must not travel

    result = gpu_run.agent(
        "AGENT_SETTING=carried SLURM_JOB_NAME=evil SLURM_CONF=/evil "
        'CUDA_VISIBLE_DEVICES=7 "$VIBESYS_GPU" -- env'
    )

    assert result.exit_code == 0, result.output
    job = parse_env(result.output)
    # The agent's own variables travel ...
    assert job["AGENT_SETTING"] == "carried"
    # ... the host's baseline replaces the container's identity ...
    assert job["HOME"] == os.environ["HOME"]
    assert job["PATH"] == os.environ["PATH"]
    assert job["PATH"] != container["PATH"]
    for name in ("PYTHONPATH", "HOSTNAME"):
        assert job.get(name) != container.get(name) or name not in container
    # ... Slurm, not the agent, decides what Slurm and the devices look like ...
    assert job["SLURM_JOB_NAME"].startswith("vibesys-gpu-")
    assert job.get("SLURM_CONF") != "/evil"
    assert job["CUDA_VISIBLE_DEVICES"] not in {"", "7"}
    # ... and neither a host secret nor the broker's capability reaches the job.
    assert HOST_CANARY_ENV not in job
    assert HOST_CANARY_VALUE not in result.output
    assert not [name for name in job if name.startswith("VIBESYS_COMMAND_BROKER_")]


def test_the_host_secret_exists_in_the_test_process(gpu_run: OpenRun) -> None:
    """Guards the guard: the leak checks mean something only if the host holds the secret."""
    assert os.environ[HOST_CANARY_ENV] == HOST_CANARY_VALUE
    assert gpu_run.workspace.exists()


def test_a_gate_job_sees_none_of_the_hosts_environment(
    run: OpenRun, request: pytest.FixtureRequest
) -> None:
    expect_failure_for(request, run, "slurm-gpu", 1602)
    run.set_mode("accuracy", "env")

    result = run.gate("accuracy")

    assert result.exit_code == 0, result.output
    job = parse_env(result.output)
    assert HOST_CANARY_ENV not in job
    assert HOST_CANARY_VALUE not in result.output


def test_a_host_that_is_itself_inside_a_slurm_allocation_still_runs_gates_as_new_jobs(
    slurm_cluster: SlurmCluster,
    workdir: Path,
    agent_image_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # VibeSys started from an ``salloc`` shell inherits the allocation's variables.
    monkeypatch.setenv("SLURM_JOB_ID", "424242")
    monkeypatch.setenv("SLURM_JOBID", "424242")
    monkeypatch.setenv("SLURM_STEP_ID", "0")
    with open_run("slurm-gpu", slurm_cluster, workdir, agent_image_id) as run:
        result = run.gate("accuracy")

        assert result.exit_code == 0, result.output
        assert "accuracy ok" in result.output
        assert slurm_cluster.queue() == []
