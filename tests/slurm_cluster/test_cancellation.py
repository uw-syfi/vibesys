"""Stopping a client or the container cancels the real Slurm job, with the documented status."""

from __future__ import annotations

import signal
import threading
from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.cluster import HANG_GUARD_SECONDS
from tests.slurm_cluster.harness import in_background

if TYPE_CHECKING:
    from tests.slurm_cluster.cluster import SlurmCluster
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster

# Matches the Python process of the broker client (not the shell that starts it).
_CLIENT = "^python3 .*vibesys-g(pu|ate)"


def _hold(run: OpenRun) -> str:
    """The agent command that starts a job which runs until it is cancelled."""
    if run.session.view.env_kind == "slurm-gpu":
        return '"$VIBESYS_GPU" -- sleep 600'
    run.set_mode("accuracy", "hold")
    return f"{run.launcher()} --gate accuracy"


def _signal_client(run: OpenRun, number: signal.Signals) -> None:
    run.agent(f"kill -{number.value} $(pgrep -f '{_CLIENT}')")


@pytest.mark.parametrize(
    ("number", "status"), [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGKILL, 137)]
)
def test_signalling_the_agents_client_cancels_the_job_and_gives_the_shell_status(
    run: OpenRun, slurm_cluster: SlurmCluster, number: signal.Signals, status: int
) -> None:
    pending = in_background(lambda: run.agent(_hold(run)))
    job = slurm_cluster.wait_for_job("")

    _signal_client(run, number)
    result = pending.result(timeout=HANG_GUARD_SECONDS)

    # SIGKILL gives the client no chance to speak: the broker sees only a dropped connection.
    assert result.exit_code == status, result.output
    slurm_cluster.wait_for_empty_queue()
    assert slurm_cluster.job_state(job) == "CANCELLED"


def test_cancelling_the_agents_command_stops_its_client_and_its_job(
    run: OpenRun, slurm_cluster: SlurmCluster
) -> None:
    cancel = threading.Event()
    pending = in_background(lambda: run.agent(_hold(run), cancel=cancel))
    job = slurm_cluster.wait_for_job("")

    cancel.set()
    result = pending.result(timeout=HANG_GUARD_SECONDS)

    assert result.cancelled
    slurm_cluster.wait_for_empty_queue()
    assert slurm_cluster.job_state(job) == "CANCELLED"


def test_a_job_the_operator_cancels_ends_the_agents_command_with_a_failure(
    gpu_run: OpenRun, slurm_cluster: SlurmCluster
) -> None:
    pending = in_background(lambda: gpu_run.agent('"$VIBESYS_GPU" -- sleep 600'))
    job = slurm_cluster.wait_for_job("")

    slurm_cluster.slurm("scancel", job)
    result = pending.result(timeout=HANG_GUARD_SECONDS)

    assert result.exit_code not in (0, None)
    slurm_cluster.wait_for_empty_queue()
