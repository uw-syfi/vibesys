"""``SlurmJobRunner.run`` cancels the job it submitted however its wait ends.

r14: Ctrl-C reaches the transport processes of the foreground process group,
so a poll can fail mid-wait. ``run`` used to raise that failure and leave its
job queued; it submitted the job, so nobody else could cancel it.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_slurm.api import (
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmError,
    SlurmJobRequest,
    SlurmJobRunner,
)

# test-isolation: the Fake connector is an executable test double outside the library API.
from vs_slurm.fake_connector import JOB_ID, handle, recorded_commands

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

# A transport process killed by SIGINT, as Ctrl-C kills an in-flight `ssh`.
_KILLED_BY_SIGINT = 255


def _killed_transport(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, _KILLED_BY_SIGINT, "", "")


def _timed_out_transport(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    raise subprocess.TimeoutExpired(list(argv), 1.0)


def _interrupted_transport(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    del argv
    raise KeyboardInterrupt


_FAILURES: dict[str, tuple[Callable[[Sequence[str]], object], type[BaseException]]] = {
    "killed": (_killed_transport, SlurmError),
    "timed-out": (_timed_out_transport, SlurmError),
    "interrupted": (_interrupted_transport, KeyboardInterrupt),
}


class _Cluster:
    """The pending Fake cluster behind a connector that fails chosen commands."""

    def __init__(self, state: Path, failure: Callable[[Sequence[str]], object]) -> None:
        self.state = state
        self._failure = failure
        self.polls_before_failure = 0
        self.fail_scancel = False

    def process(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        assert stdin is not None
        request = json.loads(stdin)
        command = str(request.get("command", ""))
        if command.startswith("squeue"):
            if self.polls_before_failure == 0:
                return self._failure(argv)  # ty: ignore[invalid-return-type]
            self.polls_before_failure -= 1
        if command.startswith("scancel") and self.fail_scancel:
            handle(self.state, request)
            return _killed_transport(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(handle(self.state, request)))


def _run(tmp_path: Path, cluster: _Cluster) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = SlurmConfig(
        name="fake",
        remote_workspace_root="/remote/runs",
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
    )
    runner = SlurmJobRunner(config, process=cluster.process, pause=lambda _seconds: None)
    runner.run(SlurmJobRequest(workspace=workspace, command=("run-benchmark",)))


@settings(max_examples=12, deadline=None)
@given(polls=st.integers(min_value=0, max_value=3), kind=st.sampled_from(sorted(_FAILURES)))
def test_a_failed_poll_cancels_the_submitted_job(
    tmp_path_factory: pytest.TempPathFactory, polls: int, kind: str
) -> None:
    state = tmp_path_factory.mktemp("cluster")
    failure, raised = _FAILURES[kind]
    cluster = _Cluster(state, failure)
    cluster.polls_before_failure = polls

    with pytest.raises(raised):
        _run(state, cluster)

    assert recorded_commands(state).count(f"scancel {JOB_ID}") == 1


def test_a_failed_cancel_is_reported_on_the_failure_it_follows(tmp_path: Path) -> None:
    cluster = _Cluster(tmp_path, _killed_transport)
    cluster.fail_scancel = True

    with pytest.raises(SlurmError) as raised:
        _run(tmp_path, cluster)

    assert recorded_commands(tmp_path).count(f"scancel {JOB_ID}") == 1
    assert any(
        f"Slurm job {JOB_ID} may still be queued or running" in note
        for note in raised.value.__notes__
    )
