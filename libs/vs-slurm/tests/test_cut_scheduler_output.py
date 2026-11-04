"""A scheduler answer cut off mid-line never names another job or another state.

An answer that lost its tail in transit must read as unknown, never as another answer:
``Submitted batch job 5000`` cut after ``50`` names job 50, someone else's job, and a
``sacct`` lookup cut to nothing, or cut at a line break, must not let one operation be
submitted twice. The cut may fall anywhere, including before the first byte and exactly
at a line break.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import ManualClock, examples
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
from vs_slurm.fake_connector import FakeConnector, recorded_commands

# test-isolation: public wiring constructs the production cluster.
from vs_slurm.wiring import SlurmCluster

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

type Answer = Callable[[dict[str, object]], dict[str, object]]
# How much of an answer survives: that many characters, or all but its end line.
type Kept = int | Literal["before_end"]

_PROGRAMS = ("sbatch", "squeue", "sacct")


def _program(command: str) -> str | None:
    tokens = shlex.split(command)
    if "sbatch" in tokens:
        return "sbatch"
    return tokens[0] if tokens and tokens[0] in _PROGRAMS else None


def _cut(stdout: str, kept: Kept) -> str:
    """A strict prefix of ``stdout``: lost bytes, an empty answer, or a cut at a line break."""
    if kept == "before_end":
        return stdout[: stdout.rstrip("\n").rfind("\n") + 1]
    return stdout[: min(kept, len(stdout) - 1)]


def _cluster(base: Path, cuts: dict[str, Kept]) -> tuple[SlurmCluster, SlurmJobRequest, Path]:
    """A production cluster whose first answer from each program in ``cuts`` is cut short."""
    connector = FakeConnector(base / "connector")
    pending = dict(cuts)

    def cut_once(request: dict[str, object], answer: Answer) -> dict[str, object]:
        response = answer(request)
        program = _program(str(request.get("command", "")))
        if program in pending and response["stdout"]:
            return {**response, "stdout": _cut(str(response["stdout"]), pending.pop(program))}
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
        processes=connector,
        clock=ManualClock(),
    )
    cluster = SlurmCluster(runner, state_root=base / "identity")
    return cluster, SlurmJobRequest(workspace=workspace, command=("true",)), connector.state


_KEPT: st.SearchStrategy[Kept] = (
    st.just(0) | st.just("before_end") | st.integers(1, 4) | st.integers(1, 64)
)


# Every program's answer is cut in each pull request; anywhere at nightly depth. The
# executing Fake runs a job at sbatch, the slow part, so the pull-request tier tries one.
@pytest.mark.parametrize("program", _PROGRAMS)
@settings(max_examples=examples(pr=1, nightly=30), deadline=None)
@given(kept=_KEPT)
def test_a_cut_answer_never_names_another_job_or_state(
    tmp_path_factory: pytest.TempPathFactory, program: str, kept: Kept
) -> None:
    cluster, request, _ = _cluster(tmp_path_factory.mktemp("cut"), {program: kept})

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


@settings(max_examples=examples(pr=1, nightly=20), deadline=None)
@given(kept=_KEPT)
def test_a_cut_lookup_never_lets_a_lost_submission_run_twice(
    tmp_path_factory: pytest.TempPathFactory, kept: Kept
) -> None:
    """sbatch's reply is lost, and the lookup by name that follows is cut short.

    The job already ran and left the queue, so only accounting knows it. Whatever the
    cut accounting answer reads as, the operation is submitted once.
    """
    cluster, request, state = _cluster(
        tmp_path_factory.mktemp("lookup"), {"sbatch": 0, "sacct": kept}
    )

    outcomes = [cluster.submit(request, operation_id="op") for _ in range(3)]

    submissions = [command for command in recorded_commands(state) if "sbatch" in command]
    assert len(submissions) == 1
    assert all(isinstance(item, ClusterUnknown | ClusterSubmitted) for item in outcomes)
    submitted = outcomes[-1]
    assert isinstance(submitted, ClusterSubmitted)
    assert isinstance(submitted.handle, SlurmJobHandle)
    assert submitted.handle.job_id == "5000"
