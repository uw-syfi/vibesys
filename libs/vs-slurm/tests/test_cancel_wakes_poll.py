"""A cancel ends the runner's poll pause when it is set, not when the pause runs out.

Regression for #1633: the pause between polls was ``time.sleep``, so a cancel
set during it was read only after the whole interval. The events here stand in
for the cancel path: each is set from inside the pause it must end, so the
tests never measure time. A pause that ignored the event would run on until the
job timeout (two seconds here) and fail the assertions instead of hanging.
"""

from __future__ import annotations

import json
import subprocess
import threading
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_slurm.api import (
    ClusterSubmitted,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmError,
    SlurmJobRequest,
    SlurmJobRunner,
)

# test-isolation: the Fake connector is an executable test double outside the library API.
from vs_slurm.fake_connector import JOB_ID, handle, recorded_commands

# test-isolation: SlurmCluster is the cluster the gate command is wired to; the api exports its interface only.
from vs_slurm.wiring import SlurmCluster

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_POLL_INTERVAL_SECONDS = 3600.0
# The hang guard: a pause that ignores the cancel ends the wait at this timeout.
_JOB_TIMEOUT_SECONDS = 2


class _CancelDuringPause(threading.Event):
    """An event whose setter is the cancel path, arriving during a chosen pause."""

    def __init__(self, *, pauses_before_cancel: int) -> None:
        super().__init__()
        self._pauses_before_cancel = pauses_before_cancel
        self.pauses: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.pauses.append(timeout)
        if len(self.pauses) > self._pauses_before_cancel:
            self.set()
        return self.is_set()


class _Cluster:
    """The Fake cluster behind a connector; its job never leaves the queue by itself."""

    def __init__(self, state: Path) -> None:
        self.state = state

    def process(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        assert stdin is not None
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(handle(self.state, json.loads(stdin)))
        )


def _runner(cluster: _Cluster, poll_interval: float) -> SlurmJobRunner:
    config = SlurmConfig(
        name="fake",
        remote_workspace_root="/remote/runs",
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        poll_interval_seconds=poll_interval,
        job_timeout_seconds=_JOB_TIMEOUT_SECONDS,
    )
    return SlurmJobRunner(config, process=cluster.process)


def _request(directory: Path) -> SlurmJobRequest:
    workspace = directory / "workspace"
    workspace.mkdir(exist_ok=True)
    return SlurmJobRequest(workspace=workspace, command=("run-benchmark",))


def _status_reads(state: Path) -> int:
    return sum(command.startswith("squeue") for command in recorded_commands(state))


def test_a_cancel_set_during_the_pause_ends_the_wait_without_the_interval_passing(
    tmp_path: Path,
) -> None:
    runner = _runner(_Cluster(tmp_path), _POLL_INTERVAL_SECONDS)
    job = runner.submit(_request(tmp_path))
    cancel = _CancelDuringPause(pauses_before_cancel=0)

    with pytest.raises(SlurmError, match="was cancelled"):
        runner.wait(job, cancel_event=cancel, timeout_seconds=_JOB_TIMEOUT_SECONDS)

    # The pause was spent on the event, for at most the interval, and it ended there.
    assert len(cancel.pauses) == 1
    assert recorded_commands(tmp_path).count(f"scancel {JOB_ID}") == 1


@settings(max_examples=15, deadline=None)
@given(
    pauses_before_cancel=st.integers(min_value=0, max_value=4),
    poll_interval=st.floats(min_value=0.01, max_value=60.0),
)
def test_a_cancel_is_seen_at_the_first_pause_after_it_is_set(
    tmp_path_factory: pytest.TempPathFactory, pauses_before_cancel: int, poll_interval: float
) -> None:
    state = tmp_path_factory.mktemp("cluster")
    runner = _runner(_Cluster(state), poll_interval)
    job = runner.submit(_request(state))
    cancel = _CancelDuringPause(pauses_before_cancel=pauses_before_cancel)

    with pytest.raises(SlurmError, match="was cancelled"):
        runner.wait(job, cancel_event=cancel, timeout_seconds=_JOB_TIMEOUT_SECONDS)

    # One status read before each pause; the cancel ends the pause it arrived in.
    assert len(cancel.pauses) == pauses_before_cancel + 1
    assert _status_reads(state) == pauses_before_cancel + 1
    assert all(pause is not None and pause <= poll_interval for pause in cancel.pauses)
    assert recorded_commands(state).count(f"scancel {JOB_ID}") == 1


@settings(max_examples=10, deadline=None)
@given(inspections_before_cancel=st.integers(min_value=0, max_value=3))
def test_a_cancel_asks_the_scheduler_before_it_records_the_tombstone(
    tmp_path_factory: pytest.TempPathFactory, inspections_before_cancel: int
) -> None:
    """The job is cancelled in the cancel's first round trip, not after its bookkeeping.

    Over SSH every command is seconds; the durable tombstone takes four of them
    and used to precede the ``scancel`` (#1633).
    """
    state = tmp_path_factory.mktemp("cluster")
    runner = _runner(_Cluster(state), _POLL_INTERVAL_SECONDS)
    cluster = SlurmCluster(runner, state_root=state / "cache")
    submitted = cluster.submit(_request(state), operation_id="op1")
    assert isinstance(submitted, ClusterSubmitted)
    for _ in range(inspections_before_cancel):
        cluster.inspect(submitted.handle)
    before = len(recorded_commands(state))

    cluster.cancel(submitted.handle)

    commands = recorded_commands(state)[before:]
    scancel = commands.index(f"scancel {JOB_ID}")
    tombstone = next(
        i for i, command in enumerate(commands) if ".cluster-cancelled.pending" in command
    )
    assert scancel < tombstone
    assert commands.count(f"scancel {JOB_ID}") == 1
    # Nothing but reads precedes the scancel, so it is not delayed by uploads.
    assert not any(command.startswith("mkdir") for command in commands[:scancel])
