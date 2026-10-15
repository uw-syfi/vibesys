"""A run that exits cancels the Slurm jobs its evaluations submitted."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import TYPE_CHECKING

import pytest
from tests.support.docker_environment import host_container_backend
from tests.support.real_git import NO_GITHUB, REAL_GIT, REAL_PROCESSES
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.task_project import write_task
from tests.vibesys.orchestration.plugin import EmptyOptions, capability_plugin

from vibesys.api._session import run_plugin
from vibesys.api.testing import FakeProfilerPreflight
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_task
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import RunRequest
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore, FakeDockerBuildRunner
from vs_core.api import StopMode
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api.core import RunTiming
from vs_runtime.api.infrastructure import RunEnvironmentSpec, RunStopped
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api import OsThreads, SystemClock, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import ManualClock
from vs_slurm.fake_connector import HOLD_FILE, SUBMITTED_FILE, executing_cluster, recorded_commands

if TYPE_CHECKING:
    from pathlib import Path

_PLUGIN = capability_plugin("slurm-exit", strategy=SkeletonStrategy.baseline_probe())


def _write_slurm_config(path: Path, state: Path, transport: str) -> None:
    cluster = [sys.executable, "-m", "vs_slurm.fake_connector", str(state)]
    if transport == "connector":
        table = f'{{ kind = "connector", command = {json.dumps(cluster)} }}'
    else:
        # Production's transport: the evaluation gate reaches the cluster only
        # through the run's host-side broker.
        table = (
            f'{{ kind = "ssh", host = "fake", ssh_command = {json.dumps([*cluster, "ssh"])}, '
            f"rsync_command = {json.dumps([*cluster, 'rsync'])} }}"
        )
    # A one-hour poll interval: the job leaves the queue only through scancel.
    path.write_text(
        "[slurm]\n"
        'name = "fake"\n'
        f"remote_workspace_root = {json.dumps(str(state.parent / 'remote'))}\n"
        "poll_interval_seconds = 3600.0\n"
        f"transport = {table}\n",
        encoding="utf-8",
    )


def _write_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "queue.py").write_text("VALUE = 1\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n'
        '[benchmark]\ncommand = ["run-benchmark"]\nresult_protocol = 2\n',
        objective="Improve the queue.\n",
    )


def _request(project_root: Path, config_path: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id=_PLUGIN.id, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "slurm-exit"}}),
        input_bundle=load_task(project_root),
        objective="Improve the queue.",
        exp_name="slurm-exit",
        agent_backend="cli",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
        run_environment=RunEnvironmentSpec(
            "slurm", {"config_path": str(config_path), "build_runner": FakeDockerBuildRunner()}
        ),
    )


def test_a_stopped_run_cancels_a_pending_benchmark_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Budget: a whole run over a fake cluster in a child process, reached by the production
    transport (ssh); the connector transport differs only in how the broker spawns the cluster."""
    # The agent container needs the credential its CLI would start with.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic-key")
    state = executing_cluster(tmp_path / "cluster")
    (state / HOLD_FILE).touch()
    os.mkfifo(state / SUBMITTED_FILE)
    config_path = tmp_path / "slurm.toml"
    _write_slurm_config(config_path, state, "ssh")
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(REAL_PROCESSES),
    )
    submitted_ids: list[str] = []

    async def exercise() -> None:
        run = asyncio.create_task(
            run_plugin(
                _request(project_root, config_path),
                integration,
                _PLUGIN,
                EmptyOptions(),
                agent_client_factory=lambda **_: FakeAgentClient(
                    capabilities=AgentCapabilities(provider_session_resume=True),
                    threads=OsThreads(),
                ),
                backend_factory=host_container_backend,
                stop_timer=SystemClock().sleep,
                timing=RunTiming(SystemClock(), 60.0, OsThreads()),
                invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
                blocking=ThreadBlockingRunner(),
                network=UnixNetwork(),
                profiler_preflight=FakeProfilerPreflight(),
            )
        )
        # The run's input-baseline measurement is queued on the held cluster; stopping the run
        # must cancel it there instead of leaving it to the cluster's time limit.
        submitted = await asyncio.to_thread((state / SUBMITTED_FILE).read_text, encoding="utf-8")
        assert submitted.isdigit()
        submitted_ids.append(submitted)
        integration.control.request_stop(StopMode.CANCEL)
        with pytest.raises(RunStopped):
            await run

    try:
        asyncio.run(exercise())
    finally:
        integration.close()

    assert submitted_ids
    assert all(f"scancel {job_id}" in recorded_commands(state) for job_id in submitted_ids)
