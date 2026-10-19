"""A local Slurm run reports its workspace-root probe as a startup stage, and its queue wait as it goes."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import (
    EnvironmentStartup,
    EnvironmentStartupStage,
    EnvironmentStartupState,
    EnvironmentWaitCondition,
    RunEnvironmentRequest,
    SlurmEnvironment,
)
from vs_runtime.api.testing import idle_environment_system
from vs_sandbox.api.slurm import load_slurm_operator_settings
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api.testing import ManualClock, SimThreads
from vs_slurm.api import FakeConnector, SlurmError, SlurmJobStatus

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "run-in-a-queue"
# test-isolation: the first attempt's scheduler identity, which the Fake scripts by operation id.
_OPERATION_ID = "workspace-root-probe-" + hashlib.sha256(_RUN_ID.encode()).hexdigest()[:32]
_POLL_SECONDS = 10.0
_PROBING = EnvironmentStartupStage.PROBING_WORKSPACE


def _environment(tmp_path: Path, *, queue_timeout_seconds: int | None = None) -> SlurmEnvironment:
    queue = (
        ""
        if queue_timeout_seconds is None
        else f"queue_timeout_seconds = {queue_timeout_seconds}\n"
    )
    config = tmp_path / "slurm.toml"
    config.write_text(
        f'''[slurm]
name = "test-cluster"
remote_workspace_root = "{tmp_path / "remote"}"
poll_interval_seconds = {_POLL_SECONDS}
{queue}
[slurm.transport]
kind = "local"
shell_command = ["fake-connector", "shell"]
rsync_command = ["fake-connector", "rsync"]
''',
        encoding="utf-8",
    )
    return SlurmEnvironment(load_slurm_operator_settings(config))


def _request(
    tmp_path: Path, connector: FakeConnector, observed: list[EnvironmentStartup]
) -> RunEnvironmentRequest:
    clock = ManualClock()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The scheduler wait sleeps on the threads' clock and measures on the request's: one timeline.
    threads = SimThreads()
    return RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=workspace,
        ref_dir=None,
        backend=FakeComputeBackend(threads=threads),
        agent_backend="stub",
        cli_provider=None,
        run_id=_RUN_ID,
        framework_root=tmp_path,
        startup=observed.append,
        clock=clock,
        system=replace(
            idle_environment_system(),
            threads=_ClockedThreads(threads, clock),
            slurm_processes=connector,
        ),
    )


class _ClockedThreads:
    """Threads whose sleeps also move the request's clock, so waits are measurable."""

    def __init__(self, inner: SimThreads, clock: ManualClock) -> None:
        self._inner = inner
        self._clock = clock

    def sleep(self, seconds: float) -> None:
        self._clock.advance(seconds)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_the_probe_reports_how_long_it_queued_then_ran_and_when_it_finished(
    tmp_path: Path,
) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    connector.script(
        _OPERATION_ID,
        states=(
            SlurmJobStatus.PENDING,
            SlurmJobStatus.PENDING,
            SlurmJobStatus.RUNNING,
            SlurmJobStatus.COMPLETED,
        ),
    )
    observed: list[EnvironmentStartup] = []

    _environment(tmp_path).prepare(_request(tmp_path, connector, observed))

    first, *waits, last = observed
    assert first == EnvironmentStartup(stage=_PROBING)
    assert last.state is EnvironmentStartupState.FINISHED
    assert {item.state for item in waits} == {EnvironmentStartupState.WAITING}
    # Queued while the scheduler held it, then running; the wait is one timeline from the first poll.
    conditions = [item.condition for item in waits]
    queued, running = EnvironmentWaitCondition.QUEUED, EnvironmentWaitCondition.RUNNING
    assert conditions[:2] == [queued, queued]
    assert conditions[2] is running
    assert [item.elapsed_seconds for item in waits][:3] == [0.0, _POLL_SECONDS, 2 * _POLL_SECONDS]
    elapsed = [item.elapsed_seconds or 0.0 for item in waits]
    assert elapsed == sorted(elapsed)
    assert last.elapsed_seconds == 2 * _POLL_SECONDS


def test_a_probe_that_is_never_scheduled_reports_its_queue_but_no_finish(tmp_path: Path) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    connector.script(_OPERATION_ID, states=(SlurmJobStatus.PENDING,))
    observed: list[EnvironmentStartup] = []

    with pytest.raises(SlurmError, match="never scheduled"):
        _environment(tmp_path, queue_timeout_seconds=30).prepare(
            _request(tmp_path, connector, observed)
        )

    assert observed[0] == EnvironmentStartup(stage=_PROBING)
    assert [item.state for item in observed[1:]] == [EnvironmentStartupState.WAITING] * (
        len(observed) - 1
    )
    assert {item.condition for item in observed[1:]} == {EnvironmentWaitCondition.QUEUED}
    assert [item.elapsed_seconds for item in observed[1:]] == sorted(
        item.elapsed_seconds or 0.0 for item in observed[1:]
    )
