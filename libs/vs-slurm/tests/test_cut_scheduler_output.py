"""A scheduler answer cut off mid-line never names another job or another state.

Slurm prints every ``sbatch``, ``squeue`` and ``sacct`` line whole, newline included. An
answer that stops mid-line lost its tail in transit: ``Submitted batch job 5000`` cut after
``50`` names job 50, someone else's job, which the cluster would then watch and cancel
while job 5000 ran unwatched.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import examples
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitted,
    ClusterUnknown,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
)

# test-isolation: public executable transport Fake answers as the scheduler does.
from vs_slurm.fake_connector import FakeConnector

# test-isolation: public wiring constructs the production cluster.
from vs_slurm.wiring import SlurmCluster

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

type Answer = Callable[[dict[str, object]], dict[str, object]]

_PROGRAMS = ("sbatch", "squeue", "sacct")


def _cluster(base: Path, program: str, lost: int) -> tuple[SlurmCluster, SlurmJobRequest]:
    """A production cluster whose first ``program`` answer loses its last ``lost`` characters."""
    connector = FakeConnector(base / "connector")
    pending = {program}

    def cut_once(request: dict[str, object], answer: Answer) -> dict[str, object]:
        response = answer(request)
        tokens = shlex.split(str(request.get("command", "")))
        stdout = str(response["stdout"])
        if stdout and pending & set(tokens[:1] + (["sbatch"] if "sbatch" in tokens else [])):
            pending.clear()
            return {**response, "stdout": stdout[: max(0, len(stdout) - lost)]}
        return response

    connector.intercept(cut_once)
    remote = base / "remote"
    remote.mkdir()
    workspace = base / "workspace"
    workspace.mkdir()
    runner = SlurmJobRunner(
        SlurmConfig(
            name="cut",
            remote_workspace_root=str(remote),
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        ),
        process=connector,
        clock=lambda: 0.0,
    )
    cluster = SlurmCluster(runner, state_root=base / "identity")
    return cluster, SlurmJobRequest(workspace=workspace, command=("true",))


# Losing two to four characters of ``Submitted batch job 5000\n`` leaves a shorter number;
# every program is cut that way in each pull request, and anywhere at nightly depth.
@pytest.mark.parametrize("program", _PROGRAMS)
@settings(max_examples=examples(pr=2, nightly=30), deadline=None)
@given(lost=st.integers(2, 4) | st.integers(1, 30))
def test_a_cut_answer_never_names_another_job_or_state(
    tmp_path_factory: pytest.TempPathFactory, program: str, lost: int
) -> None:
    cluster, request = _cluster(tmp_path_factory.mktemp("cut"), program, lost)

    outcomes = [cluster.submit(request, operation_id="op") for _ in range(3)]
    submitted = next(item for item in outcomes if isinstance(item, ClusterSubmitted))
    readings = [cluster.inspect("op") for _ in range(3)]

    # The executing Fake numbers its first job 5000 and runs it to completion at sbatch.
    assert all(isinstance(item, ClusterUnknown | ClusterSubmitted) for item in outcomes)
    assert isinstance(submitted.handle, SlurmJobHandle)
    assert submitted.handle.job_id == "5000"
    for reading in readings:
        assert isinstance(reading, ClusterObservation | ClusterUnknown)
        assert reading.job_id in {None, "5000"}
        if isinstance(reading, ClusterObservation):
            assert reading.status is SlurmJobStatus.COMPLETED
    assert isinstance(readings[-1], ClusterObservation)
