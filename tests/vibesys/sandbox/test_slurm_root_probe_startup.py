"""The workspace-root probe never gates run startup.

Regression: on a full cluster, the root session's ``prepare`` waited for the
1-CPU workspace-root probe job to be scheduled. No planner or implementer
started, the operator log stayed silent after "experiments gate open", and the
run failed after the 1800 s queue deadline.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import RunEnvironmentRequest, SlurmEnvironment
from vs_runtime.api.testing import idle_environment_system
from vs_sandbox.api.slurm import load_slurm_operator_settings
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api.testing import SimThreads, VirtualClock
from vs_slurm.api import FakeConnector, SlurmJobStatus

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "run-on-a-full-cluster"
# test-isolation: the probe's scheduler identity, which the Fake scripts by operation id.
_PROBE_OPERATION = "workspace-root-probe-" + hashlib.sha256(_RUN_ID.encode()).hexdigest()[:32]
_QUEUE_DEADLINE_SECONDS = 1800.0
# Guards against a hang only; a run that never schedules its probe stays far below it.
_VIRTUAL_LIMIT_SECONDS = 100_000.0


def _environment(tmp_path: Path) -> SlurmEnvironment:
    config = tmp_path / "slurm.toml"
    config.write_text(
        f'''[slurm]
name = "test-cluster"
remote_workspace_root = "{tmp_path / "remote"}"

[slurm.transport]
kind = "local"
shell_command = ["fake-connector", "shell"]
rsync_command = ["fake-connector", "rsync"]
''',
        encoding="utf-8",
    )
    return SlurmEnvironment(load_slurm_operator_settings(config))


def _request(
    tmp_path: Path, threads: SimThreads, connector: FakeConnector
) -> RunEnvironmentRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=workspace,
        ref_dir=None,
        backend=FakeComputeBackend(threads=threads),
        agent_backend="stub",
        cli_provider=None,
        run_id=_RUN_ID,
        framework_root=tmp_path,
        clock=threads.clock,
        system=replace(idle_environment_system(), threads=threads, slurm_processes=connector),
    )


@pytest.mark.xfail(strict=True, reason="the root prepare waits for the probe job to be scheduled")
def test_root_prepare_returns_while_the_probe_job_is_never_scheduled(tmp_path: Path) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    connector.script(_PROBE_OPERATION, states=(SlurmJobStatus.PENDING,), pending_reason="Resources")
    threads = SimThreads(VirtualClock(limit=_VIRTUAL_LIMIT_SECONDS))
    request = _request(tmp_path, threads, connector)
    started = threads.clock.now()

    def start_the_run() -> float:
        _environment(tmp_path).prepare(request)
        return threads.clock.now() - started

    try:
        waited = threads.run(start_the_run)
    finally:
        threads.close()

    assert waited < _QUEUE_DEADLINE_SECONDS
