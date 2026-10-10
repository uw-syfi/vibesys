"""``vibesys-gpu`` GPU requests are scheduled by Slurm against the node's (fake) GRES."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.cluster import HANG_GUARD_SECONDS, wait_until
from tests.slurm_cluster.harness import in_background, parse_env

if TYPE_CHECKING:
    from tests.slurm_cluster.cluster import SlurmCluster
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster


def _devices(output: str) -> list[str]:
    return [d for d in parse_env(output)["CUDA_VISIBLE_DEVICES"].split(",") if d]


@pytest.mark.parametrize("gpus", [1, 2, 3, 4])
def test_the_job_gets_exactly_the_requested_number_of_gpus(gpu_run: OpenRun, gpus: int) -> None:
    result = gpu_run.agent(f'"$VIBESYS_GPU" --gpus {gpus} -- env')

    assert result.exit_code == 0, result.output
    # Slurm, not the agent's empty CUDA_VISIBLE_DEVICES, names the devices.
    assert len(_devices(result.output)) == gpus
    assert f"gpus={gpus}" in result.output


def test_a_request_without_a_count_gets_one_gpu(gpu_run: OpenRun) -> None:
    result = gpu_run.agent('"$VIBESYS_GPU" -- env')

    assert len(_devices(result.output)) == 1


@pytest.mark.parametrize("flags", ["--gpus 5", "--gpus 0", "--time 11"])
def test_a_request_beyond_the_operator_limits_is_refused_without_a_job(
    gpu_run: OpenRun, slurm_cluster: SlurmCluster, flags: str
) -> None:
    result = gpu_run.agent(f'"$VIBESYS_GPU" {flags} -- true')

    assert result.exit_code == 2, result.output
    assert slurm_cluster.queue() == []


def test_a_request_the_node_cannot_yet_satisfy_waits_in_the_queue_until_gpus_free_up(
    gpu_run: OpenRun, slurm_cluster: SlurmCluster
) -> None:
    holder = in_background(lambda: gpu_run.agent('"$VIBESYS_GPU" --gpus 3 -- sleep 600'))
    holder_job = slurm_cluster.wait_for_job("")

    waiter = in_background(lambda: gpu_run.agent('"$VIBESYS_GPU" --gpus 2 -- env'))
    wait_until(
        lambda: any(state == "PENDING" for _, _, state in slurm_cluster.queue()),
        what="the second request to queue",
    )
    states = {job: state for job, _, state in slurm_cluster.queue()}
    assert states.pop(holder_job) == "RUNNING"
    assert list(states.values()) == ["PENDING"]
    reason = slurm_cluster.slurm("squeue", "-h", "-t", "PENDING", "-o", "%r").stdout.strip()
    assert reason in {"Resources", "Priority"}

    slurm_cluster.slurm("scancel", holder_job)
    result = waiter.result(timeout=HANG_GUARD_SECONDS)

    assert result.exit_code == 0, result.output
    assert len(_devices(result.output)) == 2
    assert holder.result(timeout=HANG_GUARD_SECONDS).exit_code not in (0, None)
