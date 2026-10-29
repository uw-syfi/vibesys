"""A cancel ends the runner's poll pause when it is set, not when the pause runs out.

Regression for #1633: the pause between polls was ``time.sleep``, so a cancel
set during it was read only after the whole interval. The events here stand in
for the cancel path: each is set from inside the pause it must end, so the
tests never measure time. A pause that ignored the event would run on until the
job timeout (two seconds here) and fail the assertions instead of hanging.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sim.api import OutputSink, ProcessOutcome, ProcessRun, ProcessSpec
from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    ClusterCancelRequested,
    ClusterSubmitted,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmError,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
)

# test-isolation: the Fake connector is an executable test double outside the library API.
from vs_slurm.fake_connector import JOB_ID, FakeConnector, handle, recorded_commands

# test-isolation: SlurmCluster is the cluster the gate command is wired to; the api exports its interface only.
from vs_slurm.wiring import SlurmCluster

if TYPE_CHECKING:
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

    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        del output
        stdin = spec.input.decode() or None
        del timeout
        assert stdin is not None
        return ProcessOutcome(0, (json.dumps(handle(self.state, json.loads(stdin)))).encode(), b"")


def _runner(cluster: _Cluster, poll_interval: float) -> SlurmJobRunner:
    config = SlurmConfig(
        name="fake",
        remote_workspace_root="/remote/runs",
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        poll_interval_seconds=poll_interval,
        job_timeout_seconds=_JOB_TIMEOUT_SECONDS,
    )
    return SlurmJobRunner(config, processes=cluster, clock=ManualClock())


def _unpaused(seconds: float) -> None:
    """A wait with a cancel event pauses on the event, never through its ``pause``."""
    message = f"the wait paused for {seconds} s outside its cancel event"
    raise AssertionError(message)


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
        runner.wait(job, cancel_event=cancel, timeout_seconds=_JOB_TIMEOUT_SECONDS, pause=_unpaused)

    # The pause was spent on the event, for at most the interval, and it ended there.
    assert len(cancel.pauses) == 1
    assert recorded_commands(tmp_path).count(f"scancel {JOB_ID}") == 1


@settings(max_examples=examples(pr=5, nightly=15), deadline=None)
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
        runner.wait(job, cancel_event=cancel, timeout_seconds=_JOB_TIMEOUT_SECONDS, pause=_unpaused)

    # One status read before each pause; the cancel ends the pause it arrived in.
    assert len(cancel.pauses) == pauses_before_cancel + 1
    assert _status_reads(state) == pauses_before_cancel + 1
    assert all(pause is not None and pause <= poll_interval for pause in cancel.pauses)
    assert recorded_commands(state).count(f"scancel {JOB_ID}") == 1


@settings(max_examples=examples(pr=5, nightly=15), deadline=None)
@given(
    poll_interval=st.floats(min_value=1.0, max_value=3600.0),
    polls=st.integers(min_value=1, max_value=20),
)
def test_a_wait_without_a_cancel_pauses_only_through_its_pause(
    tmp_path_factory: pytest.TempPathFactory, poll_interval: float, polls: int
) -> None:
    """Every pause between polls is the caller's: hours of waiting take no real time.

    The wait once fell back to ``time.sleep`` when no pause was injected, so a caller
    that forgot one slept for real; the pause is now a required argument.
    """
    state = tmp_path_factory.mktemp("cluster")
    timeout = poll_interval * polls
    clock = ManualClock()
    config = SlurmConfig(
        name="fake",
        remote_workspace_root="/remote/runs",
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        poll_interval_seconds=poll_interval,
        job_timeout_seconds=_JOB_TIMEOUT_SECONDS,
    )
    runner = SlurmJobRunner(config, processes=_Cluster(state), clock=clock)
    job = runner.submit(_request(state))
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        clock.advance(seconds)

    outcome = runner.wait(job, timeout_seconds=timeout, pause=pause)

    assert outcome.timed_out
    assert outcome.status is SlurmJobStatus.PENDING
    assert sum(pauses) == pytest.approx(timeout)
    assert all(seconds <= poll_interval for seconds in pauses)


@settings(max_examples=examples(pr=5, nightly=10), deadline=None)
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

    cancelled = cluster.cancel(submitted.handle)

    commands = recorded_commands(state)[before:]
    assert isinstance(cancelled, ClusterCancelRequested)
    scancel = commands.index(f"scancel {JOB_ID}")
    tombstone = next(
        i for i, command in enumerate(commands) if ".cluster-cancelled.pending" in command
    )
    assert scancel < tombstone
    assert commands.count(f"scancel {JOB_ID}") == 1
    # Completed observations do not cross request boundaries. Cancellation owns
    # one fresh scheduler read even when a prior inspection used the same locator.
    assert sum(command.startswith("squeue ") for command in commands[:scancel]) == 1


def test_cancel_reuses_operation_evidence_recovered_by_the_same_request(tmp_path: Path) -> None:
    """A fresh local cache reads remote identity once before cancelling the scheduler job."""
    runner = _runner(_Cluster(tmp_path), _POLL_INTERVAL_SECONDS)
    cluster = SlurmCluster(runner, state_root=tmp_path / "first-cache")
    submitted = cluster.submit(_request(tmp_path), operation_id="recover-cancel")
    assert isinstance(submitted, ClusterSubmitted)
    recovered = SlurmCluster(runner, state_root=tmp_path / "fresh-cache")
    before = len(recorded_commands(tmp_path))

    cancelled = recovered.cancel("recover-cancel")

    commands = recorded_commands(tmp_path)[before:]
    assert isinstance(cancelled, ClusterCancelRequested)
    scancel = commands.index(f"scancel {JOB_ID}")
    reads = commands[:scancel]
    assert len(reads) == 2
    assert sum(".cluster-operation/intent.json" in command for command in reads) == 1
    assert sum(command.startswith("squeue ") for command in reads) == 1


def test_cancel_does_not_reuse_a_completed_inspection_read(tmp_path: Path) -> None:
    """A later cancellation owns a fresh scheduler read for its own decision."""
    connector = FakeConnector(tmp_path / "connector")
    connector.script("crossing-cancel", states=(SlurmJobStatus.RUNNING,))
    runner = SlurmJobRunner(
        SlurmConfig(
            name="fake",
            remote_workspace_root=str(tmp_path / "remote"),
            transport=SlurmConnectorTransport(kind="connector", command=("fake",)),
        ),
        processes=connector,
        clock=ManualClock(),
    )
    cluster = SlurmCluster(runner, state_root=tmp_path / "state")
    submitted = cluster.submit(_request(tmp_path), operation_id="crossing-cancel")
    assert isinstance(submitted, ClusterSubmitted)
    assert isinstance(submitted.handle, SlurmJobHandle)
    cluster.inspect(submitted.handle)
    before = len(recorded_commands(connector.state))

    cancelled = cluster.cancel(submitted.handle)

    commands = recorded_commands(connector.state)[before:]
    scancel = commands.index(f"scancel {submitted.handle.job_id}")
    assert isinstance(cancelled, ClusterCancelRequested)
    assert sum(command.startswith("squeue ") for command in commands[:scancel]) == 1
